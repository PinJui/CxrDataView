import argparse
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import blake3
import cv2
from botocore.exceptions import ClientError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from tqdm import tqdm

from cxr_dataset_manager.db import models as m
from cxr_dataset_manager.db.engine import new_session
from cxr_dataset_manager.settings import ORIGINAL_SET_BUCKET
from cxr_dataset_manager.storage import get_store, object_key_for_image


def _get_or_create(db, model, defaults=None, **keys):
    row = db.execute(select(model).filter_by(**keys)).scalar_one_or_none()
    if row is not None:
        return row
    row = model(**keys, **(defaults or {}))
    db.add(row)
    db.flush()
    return row


def process_and_upload_single_image(
    filepath, calc_blake3, store, object_key, image_batch_id, uploaded_keys
):
    """(Worker) Responsible for single image OpenCV examination、Hash calculation and S3 upload"""

    # 1. Image check (OpenCV)
    img = cv2.imread(str(filepath), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise RuntimeError(
            f"Opencv cannot parse the file or the file is deprecated: {filepath.name}"
        )
    height, width = img.shape[:2]

    # 2. Hash calculation (optional)
    b3_hash = None
    if calc_blake3:
        h = blake3.blake3()
        with open(filepath, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        b3_hash = h.hexdigest()

    # 3. Upload to S3
    try:
        store.client.upload_file(
            Filename=str(filepath), Bucket=ORIGINAL_SET_BUCKET, Key=object_key
        )
        # Upload successful, immediately register to shared list (Python's list.append is Thread-safe)
        uploaded_keys.append(object_key)
    except Exception as exc:
        raise RuntimeError(f"Upload to S3 failed {filepath.name}: {exc}")

    return {
        "image_batch_id": image_batch_id,
        "file_name": filepath.name,
        "height": height,
        "width": width,
        "blake3_hash": b3_hash,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Import images from a folder into an Original-set and Image-batch in the database, and upload them to S3."
    )
    parser.add_argument(
        "-d", "--dir", required=True, help="Directory containing the images to import"
    )
    parser.add_argument(
        "-s",
        "--set-name",
        required=True,
        help="Original-set name to import into (will be created if it doesn't exist)",
    )
    parser.add_argument(
        "-v", "--batch-version", required=True, help="Image-batch version (e.g., V1)"
    )
    parser.add_argument(
        "--ext",
        default=".png",
        help="File extension of images to import (default: .png)",
    )
    parser.add_argument(
        "--calc-blake3",
        action="store_true",
        help="Whether to calculate and store blake3 hash during import",
    )
    parser.add_argument(
        "--on-conflict",
        choices=["overwrite", "skip", "error"],
        default="error",
        help="Handling method when a file is already registered in the database "
        "for this image batch (default: error)",
    )
    parser.add_argument(
        "--on-image-exists",
        choices=["skip", "overwrite", "error"],
        default="error",
        help="Handling method when the bucket already holds an object with the same "
        "name for a file the database does not know (e.g. left by an earlier failed "
        "run): skip leaves it untouched and does not register the file either, "
        "overwrite backs it up and replaces it, error aborts (default: error). "
        "Files already in the database are governed by --on-conflict.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() * 4,
        help="Number of threads for parallel upload",
    )

    args = parser.parse_args()

    ext = args.ext.lower()
    if not ext.startswith("."):
        ext = "." + ext

    folder_path = Path(args.dir)
    if not folder_path.is_dir():
        print(f"[ERROR]: Directory {folder_path} does not exist.")
        return

    image_files = [
        f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() == ext
    ]
    if not image_files:
        print(f"[ERROR]: No {ext} files found in {folder_path}.")
        return

    try:
        store = get_store()
        store.ensure_buckets()
    except Exception as e:
        print(f"[ERROR]: S3 (MinIO) connection or Bucket initialization failed: {e}")
        return

    db = new_session()

    # 建立一個全域的清單，用來紀錄這次執行中「成功寫入 S3 的檔案」
    uploaded_keys = []
    # 記錄 overwrite 模式下，每個「被覆寫的原始 key」對應的備份 key
    # (原始 key -> 備份 key)。有了這份對照表，rollback 時才能把舊內容
    # 復原回去，而不是把新舊內容一起刪光光。
    backup_keys = {}

    try:
        original_set = _get_or_create(db, m.OriginalSet, name=args.set_name)
        original_set_id = original_set.id

        image_batch = _get_or_create(
            db,
            m.ImageBatch,
            original_set_id=original_set_id,
            version=args.batch_version,
        )
        image_batch_id = image_batch.id

        # 提前檢查衝突
        existing_filenames = set(
            db.execute(
                select(m.Image.file_name).where(
                    m.Image.image_batch_id == image_batch_id
                )
            )
            .scalars()
            .all()
        )
        conflicts = [f.name for f in image_files if f.name in existing_filenames]
        if conflicts:
            if args.on_conflict == "error":
                print(
                    f"\n[ERROR] Found {len(conflicts)} existing files in the database, operation aborted!"
                    "\n        Use --on-conflict skip or overwrite to decide what happens to them."
                )
                return
            elif args.on_conflict == "skip":
                print(f"\n[INFO] Skipping {len(conflicts)} existing files.")
                image_files = [
                    f for f in image_files if f.name not in existing_filenames
                ]
                if not image_files:
                    print("[INFO] Nothing left to import.")
                    return
            elif args.on_conflict == "overwrite":
                print(f"\n[INFO] Overwriting {len(conflicts)} existing files.")

        # 上面只比對資料庫，看不到「bucket 裡已經有、資料庫卻沒有」的物件——
        # 通常是之前某次執行上傳後失敗、S3 清理沒跑完留下的，或有人手動放的。
        # 直接上傳會悄悄蓋掉它們，所以跟 --on-conflict 一樣交給使用者決定。
        # 一次 list 整個前綴，而不是每個檔案各發一個 HEAD。
        existing_keys = store.list_keys(
            object_key_for_image(args.set_name, args.batch_version, ""),
            bucket=ORIGINAL_SET_BUCKET,
        )
        in_bucket_only = [
            f.name
            for f in image_files
            if f.name not in existing_filenames
            and object_key_for_image(args.set_name, args.batch_version, f.name)
            in existing_keys
        ]
        if in_bucket_only:
            sample = ", ".join(in_bucket_only[:10]) + (
                " ..." if len(in_bucket_only) > 10 else ""
            )
            if args.on_image_exists == "error":
                print(
                    f"\n[ERROR] {len(in_bucket_only)} files already exist in the bucket "
                    f"but not in the database, operation aborted: {sample}"
                    "\n        Use --on-image-exists skip or overwrite to decide what happens to them."
                )
                return
            if args.on_image_exists == "skip":
                # 跳過就是兩邊一起跳過：bucket 裡那份的內容不見得跟本機這份相同，
                # 只寫資料庫列的話，metadata 描述的會是另一張圖。
                print(
                    f"\n[INFO] Skipping {len(in_bucket_only)} files that already exist "
                    f"in the bucket (neither uploaded nor registered): {sample}"
                )
                skip_set = set(in_bucket_only)
                image_files = [f for f in image_files if f.name not in skip_set]
                if not image_files:
                    print("[INFO] Nothing left to import.")
                    return
            else:
                print(
                    f"\n[INFO] Overwriting {len(in_bucket_only)} files that exist in "
                    "the bucket but not in the database."
                )

        # 不管是哪個旗標允許的覆寫，被覆寫的舊物件都要先備份
        to_overwrite = (
            set(in_bucket_only) if args.on_image_exists == "overwrite" else set()
        )
        if args.on_conflict == "overwrite":
            to_overwrite |= set(conflicts)
        if to_overwrite:
            # --------------------------------------------------------
            # overwrite 模式的資料遺失風險:
            # 新舊檔案共用同一個 S3 key。如果直接上傳覆寫、上傳到一半
            # 又失敗，舊版 rollback 邏輯會把這個 key 整個刪掉——不只
            # 刪掉這次沒上傳完的新內容，連原本跟這次錯誤無關、已經存
            # 在很久的舊檔案也會一起消失，而且沒有備份可以救。
            #
            # 修法: 在真正覆寫之前，先把每一個即將被覆寫的舊物件複製
            # 一份到備份 key。之後不管成功或失敗:
            #   - 失敗 (rollback) 時: 把備份複製回原本的 key「還原」，
            #     而不是直接刪除，原本的舊檔案才不會憑空消失。
            #   - 成功 (commit) 時: 備份已經沒用了，刪掉即可。
            # --------------------------------------------------------
            print(
                f"[INFO] Backing up {len(to_overwrite)} existing files that will be overwritten..."
            )
            skipped_backup_files = []
            try:
                for filepath in image_files:
                    if filepath.name not in to_overwrite:
                        continue
                    object_key = object_key_for_image(
                        args.set_name, args.batch_version, filepath.name
                    )
                    backup_key = f"_rollback_backups/{object_key}.{uuid.uuid4().hex}.bak"
                    try:
                        store.client.copy_object(
                            Bucket=ORIGINAL_SET_BUCKET,
                            CopySource={
                                "Bucket": ORIGINAL_SET_BUCKET,
                                "Key": object_key,
                            },
                            Key=backup_key,
                        )
                        backup_keys[object_key] = backup_key
                    except ClientError as ce:
                        # DB 認為這個檔案存在（file_name 在 existing_filenames
                        # 裡），但 S3 上實際找不到對應的 object_key。這代表
                        # DB 紀錄跟 S3 儲存已經不同步（孤兒紀錄），並不是這
                        # 次操作造成的。既然本來就沒有東西可以備份，直接讓
                        # 這個檔案照常覆寫上傳即可 —— 不備份、也不中止整批。
                        error_code = ce.response.get("Error", {}).get("Code", "")
                        if error_code in ("NoSuchKey", "404"):
                            skipped_backup_files.append(filepath.name)
                            continue
                        raise
            except Exception as backup_e:
                # 其他非預期的備份錯誤（權限、網路等）: 這時候還沒有任何
                # 檔案被覆寫，所以只要把「已經備份成功」的那幾份清掉，
                # 直接中止即可，不需要動到任何原始檔案。
                print(
                    f"\n[ERROR] Failed to backup old files, operation aborted: {backup_e}"
                )
                for bkey in backup_keys.values():
                    try:
                        store.delete(bkey, bucket=ORIGINAL_SET_BUCKET)
                    except Exception as cleanup_e:
                        print(f"  - Failed to cleanup backup {bkey}: {cleanup_e}")
                return

            if skipped_backup_files:
                print(
                    f"[WARNING] {len(skipped_backup_files)} files were found in the database but not in S3 (DB/S3 out of sync), they will be overwritten without backup: "
                    f"{', '.join(skipped_backup_files[:10])}"
                    + (" ..." if len(skipped_backup_files) > 10 else "")
                )
            print("[INFO] Backup completed, proceeding to upload and overwrite...")

        print(f"[INFO] Ready to process and upload {len(image_files)} files...")
        records_to_insert = []
        has_error = False

        # ------------------------------------------------------------------
        # 修正重點:
        # 原本用 `with ThreadPoolExecutor(...) as executor:` 搭配 for-break，
        # break 只會停止「讀取結果」的迴圈，但 executor.submit() 在進入迴圈前
        # 就已經把所有檔案一次送進執行緒池了。當某個檔案出錯、程式 break 之
        # 後，池子裡其他還沒開始跑的任務仍會繼續被排程執行，直到離開 with
        # 區塊時自動呼叫的 shutdown(wait=True) 把它們全部跑完為止。
        #
        # 這就是為什麼「明明第幾個檔案就出錯了，卻刪除了一堆超過預期的幽靈
        # 檔案」──因為程式其實把資料夾裡幾乎所有檔案都上傳完了才停下來，
        # 不只慢，清理的數字也跟你以為「出錯前處理了幾個檔案」對不上。
        #
        # 解法: 偵測到錯誤時，改用 executor.shutdown(cancel_futures=True)，
        # 把「尚未開始執行」的任務直接取消，只留下已經在跑/已跑完的任務繼續
        # 執行完（避免和後面清理階段的 uploaded_keys 讀取產生 race
        # condition）。這樣清理的數量才會真正等於「這次實際被上傳過的檔案
        # 數」，也能讓失敗時更快中止。
        # (cancel_futures 參數需要 Python 3.9+)
        # ------------------------------------------------------------------
        executor = ThreadPoolExecutor(max_workers=args.workers)
        try:
            futures = {}
            for filepath in image_files:
                object_key = object_key_for_image(
                    args.set_name, args.batch_version, filepath.name
                )
                future = executor.submit(
                    process_and_upload_single_image,
                    filepath,
                    args.calc_blake3,
                    store,
                    object_key,
                    image_batch_id,
                    # Passing in the list reference so that the worker can append to it when upload is successful
                    uploaded_keys,
                )
                futures[future] = filepath

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Processing and uploading",
            ):
                try:
                    record = future.result()
                    records_to_insert.append(record)
                except Exception as e:
                    tqdm.write(f"\n[ERROR] {e!s}")
                    has_error = True
                    break
        finally:
            # wait=True: 等待「已經在執行中」的任務跑完，確保 uploaded_keys
            #            在進入後續清理流程前已經是最終、完整的狀態。
            # cancel_futures=True: 取消「還沒開始跑」的任務，避免出錯後還
            #            繼續浪費頻寬/時間上傳一堆之後又要刪除的檔案。
            executor.shutdown(wait=True, cancel_futures=True)

        if has_error:
            raise RuntimeError(
                "[ERROR] Error occurred during image processing or upload."
            )

        # 資料庫寫入
        if not records_to_insert:
            print(
                "[INFO] No images successfully processed and uploaded, nothing to insert into the database."
            )
            return

        if args.on_conflict in ["overwrite", "skip"]:
            stmt = insert(m.Image).values(records_to_insert)
            if args.on_conflict == "overwrite":
                stmt = stmt.on_conflict_do_update(
                    index_elements=["image_batch_id", "file_name"],
                    set_={
                        "height": stmt.excluded.height,
                        "width": stmt.excluded.width,
                        "blake3_hash": stmt.excluded.blake3_hash,
                    },
                )
            elif args.on_conflict == "skip":
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["image_batch_id", "file_name"]
                )
            db.execute(stmt)
        else:
            db.bulk_insert_mappings(m.Image, records_to_insert)

        # 只有在這裡 Commit 後，資料庫與 S3 才算正式綁定完成
        db.commit()
        print(
            f"\n[INFO] Import operation completed successfully! Total records inserted/updated: {len(records_to_insert)}"
        )
        print(
            "[INFO] Next: describe this batch with "
            f"`cxr meta images {args.set_name}@{args.batch_version}`"
        )

        # 覆寫成功、DB 也 commit 了，代表新內容已經是「正式版本」，
        # 備份的舊內容可以清掉了。
        if backup_keys:
            for backup_key in backup_keys.values():
                try:
                    store.delete(backup_key, bucket=ORIGINAL_SET_BUCKET)
                except Exception as cleanup_e:
                    print(
                        f"  - [ERROR] Failed to cleanup backup {backup_key}: {cleanup_e}"
                    )

    except (
        BaseException
    ) as e:  # 攔截所有錯誤，包含 Exception 以及 KeyboardInterrupt (Ctrl+C)
        db.rollback()

        # 根據錯誤類型印出對應訊息
        if isinstance(e, KeyboardInterrupt):
            print(
                "\n[INFO] User keyboard interrupt detected (Ctrl+C), DB writing cancelled (Rollback)."
            )
        else:
            print(f"\n[ERROR] {e} (DB Rollback Done)")

        # ==========================================
        # 執行 S3 垃圾清理 (S3 Rollback)
        # ==========================================
        if uploaded_keys:
            print(f"\n[INFO] Cleaning up {len(uploaded_keys)} affected files on S3...")
            for key in tqdm(uploaded_keys, desc="Cleaning S3 files"):
                if key in backup_keys:
                    # 這個 key 是被覆寫的舊檔案：把備份複製回去「還原」，
                    # 而不是直接刪除，這樣舊內容才不會憑空消失。
                    backup_key = backup_keys[key]
                    try:
                        store.client.copy_object(
                            Bucket=ORIGINAL_SET_BUCKET,
                            CopySource={
                                "Bucket": ORIGINAL_SET_BUCKET,
                                "Key": backup_key,
                            },
                            Key=key,
                        )
                        store.delete(backup_key, bucket=ORIGINAL_SET_BUCKET)
                    except Exception as restore_e:
                        # 還原失敗比較危險：故意保留備份不刪，並印出明確的
                        # 備份 key，方便事後手動處理，而不是默默把舊資料弄丟。
                        tqdm.write(
                            f"  - [ERROR] Failed to restore {key} (backup still exists at {backup_key}, please handle manually): {restore_e}"
                        )
                else:
                    # 這是這次執行新上傳的檔案，執行前並不存在，可以放心刪除。
                    try:
                        store.delete(key, bucket=ORIGINAL_SET_BUCKET)
                    except Exception as del_e:
                        tqdm.write(f"  - [ERROR] Failed to delete {key}: {del_e}")

            print("[INFO] S3 file cleanup completed, system state has been restored.")

        # 有些衝突檔案可能備份完之後，因為 cancel_futures 而根本沒被實際上
        # 傳到（例如錯誤發生得很早，連檔案都還沒排到就取消了）。這種 key
        # 不會出現在 uploaded_keys 裡，原始檔案完全沒被動過，所以只需要把
        # 多做的備份清掉即可，不需要「還原」。
        # 注意：這段故意放在 `if uploaded_keys:` 之外，因為 uploaded_keys
        # 為空（一個檔案都還沒上傳完就出錯）時，備份仍然可能已經建立，
        # 一樣需要清理，否則備份會變成孤兒物件留在 S3 上。
        for original_key, backup_key in backup_keys.items():
            if original_key not in uploaded_keys:
                try:
                    store.delete(backup_key, bucket=ORIGINAL_SET_BUCKET)
                except Exception as cleanup_e:
                    print(
                        f"  - [ERROR] Failed to cleanup unused backup {backup_key}: {cleanup_e}"
                    )

    finally:
        db.close()


if __name__ == "__main__":
    main()
