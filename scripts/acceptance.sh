#!/usr/bin/env bash
# 端到端驗收：從零重建整套系統，然後檢查它有沒有說謊。
#
#   ./scripts/acceptance.sh
#
# 會清空並重灌資料庫，所以只在開發環境跑。
set -uo pipefail
cd "$(dirname "$0")/.."

CXR=.venv/bin/cxr
PY=.venv/bin/python
# psql 用 docker compose 裡那個 container 的，主機不需要另外裝 client
PSQL="docker exec -i local-postgres psql -U postgres -d cxr -tA"
AUTHOR="--author-name acceptance --author-email acceptance@example.com"

pass=0; fail=0
check() {  # check <說明> <期望> <實際>
  if [ "$2" = "$3" ]; then printf '  \033[32m✓\033[0m %s\n' "$1"; pass=$((pass+1))
  else printf '  \033[31m✗\033[0m %s（期望 %s，實得 %s）\n' "$1" "$2" "$3"; fail=$((fail+1)); fi
}
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }

step "0. 服務是否活著"
$CXR db status | grep -q "ok" && check "postgres / minio 連得上" "y" "y" || check "postgres / minio 連得上" "y" "n"

step "1. 從零重建 schema 與 mock 資料"
$CXR db init --drop > /dev/null 2>&1
$CXR db seed > /dev/null 2>&1
check "影像筆數" "1058" "$($PSQL -c 'SELECT count(*) FROM images')"
check "跨來源重複影像組數（mock 資料刻意做髒）" "60" \
  "$($PSQL -c "SELECT count(*) FROM (SELECT i.blake3_hash FROM images i JOIN image_batches ib ON ib.id=i.image_batch_id GROUP BY 1 HAVING count(DISTINCT ib.original_set_id)>1) t")"
cat > /tmp/acc-minio.py <<'MINIOPY'
from cxr_dataset_manager.storage import get_store
c, n, tok = get_store().client, 0, None
while True:
    kw = {"Bucket": "original-sets", "MaxKeys": 1000}
    if tok:
        kw["ContinuationToken"] = tok
    r = c.list_objects_v2(**kw)
    n += r.get("KeyCount", 0)
    if not r.get("IsTruncated"):
        break
    tok = r["NextContinuationToken"]
print(n)
MINIOPY
check "MinIO 物件數" "1058" "$($PY /tmp/acc-minio.py 2>/dev/null)"

step "2. 單元／整合測試"
$PY -m pytest tests/ -q > /tmp/acc-pytest.log 2>&1
check "pytest 全數通過" "0" "$?"
tail -1 /tmp/acc-pytest.log | sed 's/^/     /'

step "3. dry-run 必須完全不寫資料庫"
BEFORE=$($PSQL -c "SELECT (SELECT count(*) FROM manual_sets)||'/'||(SELECT count(*) FROM manual_set_versions)||'/'||(SELECT count(*) FROM manual_set_images)")
$CXR build specs/pneumonia_train.yaml -m dryrun_probe -v V1 --dry-run > /dev/null 2>&1
AFTER=$($PSQL -c "SELECT (SELECT count(*) FROM manual_sets)||'/'||(SELECT count(*) FROM manual_set_versions)||'/'||(SELECT count(*) FROM manual_set_images)")
check "試跑前後 row 數不變（$BEFORE）" "$BEFORE" "$AFTER"

step "4. 建 train / val"
$CXR build specs/pneumonia_train.yaml -m pneumonia_train -v V1 $AUTHOR > /dev/null 2>&1
$CXR build specs/pneumonia_val.yaml   -m pneumonia_val   -v V1 $AUTHOR > /dev/null 2>&1
check "train 影像數" "394" "$($PSQL -c "SELECT count(*) FROM manual_set_images msi JOIN manual_set_versions mv ON mv.id=msi.manual_set_version_id JOIN manual_sets ms ON ms.id=mv.manual_set_id WHERE ms.name='pneumonia_train'")"
check "val 影像數" "100" "$($PSQL -c "SELECT count(*) FROM manual_set_images msi JOIN manual_set_versions mv ON mv.id=msi.manual_set_version_id JOIN manual_sets ms ON ms.id=mv.manual_set_id WHERE ms.name='pneumonia_val'")"

step "5. 可重現性：同一份 spec 重跑必須完全一樣"
$CXR build specs/pneumonia_train.yaml -m pneumonia_train -v V1-rerun $AUTHOR > /dev/null 2>&1
DIFF=$($PSQL -c "
WITH a AS (SELECT image_id FROM manual_set_images WHERE manual_set_version_id=(SELECT mv.id FROM manual_set_versions mv JOIN manual_sets ms ON ms.id=mv.manual_set_id WHERE ms.name='pneumonia_train' AND mv.version='V1')),
     b AS (SELECT image_id FROM manual_set_images WHERE manual_set_version_id=(SELECT mv.id FROM manual_set_versions mv JOIN manual_sets ms ON ms.id=mv.manual_set_id WHERE ms.name='pneumonia_train' AND mv.version='V1-rerun'))
SELECT (SELECT count(*) FROM (SELECT * FROM a EXCEPT SELECT * FROM b) x) + (SELECT count(*) FROM (SELECT * FROM b EXCEPT SELECT * FROM a) y)")
check "重跑後影像集合的差異數" "0" "$DIFF"

step "6. 獨立 SQL 檢查（繞過應用程式碼，從資料本身重算一遍）"
docker exec -i local-postgres psql -U postgres -d cxr < scripts/verify.sql > /tmp/acc-verify.log 2>&1
sed 's/^/     /' /tmp/acc-verify.log
check "verify.sql 全部歸零" "0" "$(grep -c '<<<' /tmp/acc-verify.log || true)"

step "7. 該失敗的必須失敗，而且不弄髒資料庫"
printf 'steps:\n  - {id: a, op: source, original_set: aws_images, annotation_batch: V1}\nfinal: a\n' > /tmp/neg1.yaml
$CXR build /tmp/neg1.yaml -m negtest -v V1 $AUTHOR > /dev/null 2>&1
check "漏掉 category_map 會擋下來" "1" "$?"
$CXR build specs/pneumonia_train.yaml -m pneumonia_train -v V1 $AUTHOR > /dev/null 2>&1
check "重複版本號會擋下來" "1" "$?"
printf "steps:\n  - {id: a, op: source, original_set: aws_images, image_batch: V1}\n  - {id: f, op: filter, input: a, criterion: predicate, expression: \"__import__('os').system('touch /tmp/PWNED')\"}\nfinal: f\n" > /tmp/neg2.yaml
rm -f /tmp/PWNED
$CXR build /tmp/neg2.yaml -m negtest -v V2 $AUTHOR > /dev/null 2>&1
check "predicate 不能執行任意程式碼" "absent" "$([ -f /tmp/PWNED ] && echo present || echo absent)"
check "失敗的 build 沒有留下殘跡" "0" "$($PSQL -c "SELECT count(*) FROM manual_sets WHERE name LIKE 'negtest%' OR name='dryrun_probe'")"

step "8. 匯出的內容要跟資料庫對得上"
rm -rf /tmp/acc-export && mkdir -p /tmp/acc-export
$CXR export pneumonia_train@V1 -o /tmp/acc-export -f csv > /dev/null 2>&1
check "manifest.csv 資料列數 = DB 影像數" "394" "$(( $(wc -l < /tmp/acc-export/pneumonia_train_V1_manifest.csv) - 1 ))"
$CXR export pneumonia_train@V1 -o /tmp/acc-export -f coco > /dev/null 2>&1
check "COCO images 數" "394" "$($PY -c "import json;print(len(json.load(open('/tmp/acc-export/pneumonia_train_V1_coco.json'))['images']))")"
$CXR export pneumonia_train@V1 -o /tmp/acc-export -f parquet > /dev/null 2>&1
PQ=/tmp/acc-export/manual-sets/pneumonia_train/annotations/V1
check "parquet images 數" "394" "$($PY -c "import pyarrow.parquet as pq;print(pq.read_table('$PQ/images.parquet').num_rows)")"
check "parquet 帶上 __meta__.md" "yes" "$([ -f $PQ/__meta__.md ] && echo yes || echo no)"

step "9. 每一個 CLI 指令都真的跑得起來"
# 這一節的存在理由：cxr show 曾經必定 crash，卻因為沒人跑過而活了很久。
for c in "ls sets" "ls batches" "ls categories" "ls annotators" \
         "ls manual-sets" "ls history" "db status" \
         "show pneumonia_train@V1" "spec pneumonia_train@V1" \
         "diff pneumonia_train@V1 pneumonia_val@V1" \
         "check-leakage pneumonia_train@V1 pneumonia_val@V1" \
         "validate specs/pneumonia_train.yaml" "image 1" \
         "meta manual-set pneumonia_train@V1 --view"; do
  out=$($CXR $c 2>&1)
  if [ $? -eq 0 ] && ! printf '%s' "$out" | grep -q "Traceback"; then
    check "cxr $c" "ok" "ok"
  else
    check "cxr $c" "ok" "失敗"
  fi
done
seq 1 5000 | sed 's/^/BULK_/;s/$/.png/' > /tmp/acc-list.txt
SHA=$($CXR lists add /tmp/acc-list.txt --note acceptance 2>&1 | grep -o '[0-9a-f]\{64\}' | head -1)
check "cxr lists add（5000 筆）" "64" "${#SHA}"
$CXR lists ls > /dev/null 2>&1 && check "cxr lists ls" "ok" "ok" || check "cxr lists ls" "ok" "失敗"
$CXR lists show "$SHA" -n 3 > /dev/null 2>&1 && check "cxr lists show" "ok" "ok" || check "cxr lists show" "ok" "失敗"

$CXR build specs/pneumonia_train.yaml -m rmprobe -v V1 $AUTHOR > /dev/null 2>&1
$CXR rm rmprobe@V1 --yes > /dev/null 2>&1
check "cxr rm 刪得掉" "0" "$($PSQL -c "SELECT count(*) FROM manual_sets WHERE name='rmprobe'")"
check "cxr rm 不動原始資料" "1058" "$($PSQL -c 'SELECT count(*) FROM images')"

out=$($CXR why pneumonia_train@V1 --image aws_images/V1/AWS_00004.png 2>&1)
[ $? -eq 0 ] && check "cxr why <image>" "ok" "ok" || check "cxr why <image>" "ok" "失敗"

step "10. spec 是一份檔案：存得下來（-o）、原封不動、能重建"
SPEC_KEY=$($PSQL -c "SELECT ms.name||'/annotations/'||mv.version||'/spec.yaml' FROM manual_set_versions mv JOIN manual_sets ms ON ms.id=mv.manual_set_id WHERE ms.name='pneumonia_train' AND mv.version='V1'")
check "spec 的物件路徑" "pneumonia_train/annotations/V1/spec.yaml" "$SPEC_KEY"
$CXR spec pneumonia_train@V1 -o /tmp/acc-spec.yaml > /dev/null 2>&1
# -o 是唯一的存檔路徑：直接寫檔，不經過終端機。存出來的必須跟物件儲存上
# 那份逐位元組相同。
$PY - <<'PYEOF' > /tmp/acc-spec-cmp 2>&1
from cxr_dataset_manager.storage import get_store
import pathlib
disk = pathlib.Path("/tmp/acc-spec.yaml").read_text()
print("same" if disk == get_store().get_spec("pneumonia_train", "V1") else "different")
PYEOF
check "cxr spec -o 存出的檔案與物件儲存逐位元組相同" "same" "$(cat /tmp/acc-spec-cmp)"
$CXR build /tmp/acc-spec.yaml -m specprobe -v V1 $AUTHOR > /dev/null 2>&1
# spec 也可以直接給 manual-set@版本，從物件儲存沿用那一版的配方
$CXR build pneumonia_train@V1 -m refprobe -v V1 $AUTHOR > /dev/null 2>&1
check "cxr build 吃 manual-set@版本" "186ef577cb45cbe0" "$($PSQL -c "SELECT left(mv.spec_sha256,16) FROM manual_set_versions mv JOIN manual_sets ms ON ms.id=mv.manual_set_id WHERE ms.name='refprobe'")"
out=$($CXR build /nonexistent/manual-sets/x/annotations/V1/spec.yaml -m x -v V1 $AUTHOR 2>&1)
printf '%s' "$out" | grep -q "manual-set" && check "指向物件路徑時提示改用 ref" "ok" "ok" || check "指向物件路徑時提示改用 ref" "ok" "失敗"
SPECDIFF=$($PSQL -c "
WITH a AS (SELECT image_id FROM manual_set_images WHERE manual_set_version_id=(SELECT mv.id FROM manual_set_versions mv JOIN manual_sets ms ON ms.id=mv.manual_set_id WHERE ms.name='pneumonia_train' AND mv.version='V1')),
     b AS (SELECT image_id FROM manual_set_images WHERE manual_set_version_id=(SELECT mv.id FROM manual_set_versions mv JOIN manual_sets ms ON ms.id=mv.manual_set_id WHERE ms.name='specprobe' AND mv.version='V1'))
SELECT (SELECT count(*) FROM (SELECT * FROM a EXCEPT SELECT * FROM b) x) + (SELECT count(*) FROM (SELECT * FROM b EXCEPT SELECT * FROM a) y)")
check "用存下來的 spec 重建，影像集合差異數" "0" "$SPECDIFF"
check "同一份配方指紋相同" "1" "$($PSQL -c "SELECT count(DISTINCT spec_sha256) FROM manual_set_versions mv JOIN manual_sets ms ON ms.id=mv.manual_set_id WHERE ms.name IN ('pneumonia_train','specprobe') AND mv.version IN ('V1','V1-rerun')")"

check "cxr show 有 category distribution" "ok" "$($CXR show pneumonia_train@V1 2>&1 | grep -q 'Category distribution' && echo ok || echo 缺)"
DISTOK=$($PSQL -c "
WITH d AS (
  SELECT cm.target_category_id AS tcid,
         count(DISTINCT msa.image_id) AS labelled
  FROM manual_set_cls_annotations msa
  JOIN cls_annotations a ON a.id = msa.cls_annotation_id
  JOIN manual_set_category_mappings cm
    ON cm.manual_set_version_id = msa.manual_set_version_id AND cm.category_id = a.category_id
  WHERE msa.manual_set_version_id = (SELECT mv.id FROM manual_set_versions mv JOIN manual_sets ms ON ms.id=mv.manual_set_id WHERE ms.name='pneumonia_train' AND mv.version='V1')
  GROUP BY 1)
SELECT count(*) FROM d WHERE labelled > 394")
check "沒有 target 的標註影像數超過總數" "0" "$DISTOK"

step "11. 刪掉版本時 spec.yaml 也要跟著消失"
$CXR build specs/pneumonia_train.yaml -m specrm -v V1 $AUTHOR > /dev/null 2>&1
$PY -c "from cxr_dataset_manager.storage import get_store; print('yes' if get_store().get_spec('specrm','V1') else 'no')" > /tmp/acc-specrm1 2>&1
check "建好之後 spec.yaml 在" "yes" "$(cat /tmp/acc-specrm1)"
$CXR rm specrm@V1 --yes > /dev/null 2>&1
$PY -c "from cxr_dataset_manager.storage import get_store; print('yes' if get_store().get_spec('specrm','V1') else 'no')" > /tmp/acc-specrm2 2>&1
check "刪掉之後 spec.yaml 不在" "no" "$(cat /tmp/acc-specrm2)"
$PY -c "from cxr_dataset_manager.storage import get_store; print('yes' if get_store().get_meta('manual-set','specrm','V1') else 'no')" > /tmp/acc-specrm3 2>&1
check "刪掉之後 __meta__.md 也不在" "no" "$(cat /tmp/acc-specrm3)"

step "12. 錯誤路徑要給看得懂的訊息，不能吐 traceback"
for c in "show no_such@V1" "spec no_such@V1" "export no_such@V1" "rm no_such@V1 --yes" \
         "image 99999999" "meta images no_such@V1" "export no_such@V1 -f parquet" \
         "show missing_at_sign" "validate /tmp/nope.yaml"; do
  out=$($CXR $c 2>&1)
  if [ $? -ne 0 ] && ! printf '%s' "$out" | grep -q "Traceback"; then
    check "cxr $c 乾淨失敗" "ok" "ok"
  else
    check "cxr $c 乾淨失敗" "ok" "失敗"
  fi
done

printf '\n\033[1m結果：%d 項通過，%d 項失敗\033[0m\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
