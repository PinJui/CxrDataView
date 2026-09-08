-- 獨立驗收查詢：不透過 cxr 指令，直接問資料庫。
--
-- 這些檢查刻意繞過應用程式碼——如果 ops.py 的邏輯有錯，`cxr check-leakage`
-- 會跟著一起錯；下面這些 SQL 是從資料本身重新算一遍，兩者不會錯到一起去。
--
--   psql -h localhost -p 5433 -U postgres -d cxr -f scripts/verify.sql
--
-- 每一項的「問題數」都必須是 0，不是 0 的會標上 <<< 有問題。

\set ON_ERROR_STOP on

WITH
train_val AS (
  SELECT ms.name AS set_name, msi.image_id
  FROM manual_set_images msi
  JOIN manual_set_versions mv ON mv.id = msi.manual_set_version_id
  JOIN manual_sets ms ON ms.id = mv.manual_set_id
  WHERE ms.name IN ('pneumonia_train', 'pneumonia_val')
),
checks(seq, 檢查項目, 問題數) AS (
  -- leakage 的核心承諾：同一位病患不該同時出現在 train 與 val
  SELECT 1, 'train/val 共用病患', (
    SELECT count(*) FROM (
      SELECT s.subject_id FROM image_subjects s
      JOIN train_val t ON t.image_id = s.image_id
      GROUP BY s.subject_id HAVING count(DISTINCT t.set_name) > 1) x)

  -- 病患不同、但檔案內容一模一樣，一樣是 leakage
  UNION ALL SELECT 2, 'train/val 內容相同的影像', (
    SELECT count(*) FROM (
      SELECT i.blake3_hash FROM train_val t
      JOIN images i ON i.id = t.image_id
      WHERE i.blake3_hash IS NOT NULL
      GROUP BY i.blake3_hash HAVING count(DISTINCT t.set_name) > 1) x)

  -- 衝突沒解乾淨 = 把矛盾帶進訓練資料
  UNION ALL SELECT 3, '同一(影像,類別)有多筆標註', (
    SELECT count(*) FROM (
      SELECT msa.manual_set_version_id, msa.image_id, tc.name
      FROM manual_set_cls_annotations msa
      JOIN cls_annotations a ON a.id = msa.cls_annotation_id
      JOIN manual_set_category_mappings cm
        ON cm.manual_set_version_id = msa.manual_set_version_id
       AND cm.category_id = a.category_id
      JOIN manual_set_target_categories tc ON tc.id = cm.target_category_id
      GROUP BY 1,2,3 HAVING count(*) > 1) x)

  -- schema 的複合外鍵應該擋住，這裡雙重確認
  UNION ALL SELECT 4, '標註的影像不在同版本裡', (
    (SELECT count(*) FROM manual_set_cls_annotations msa
      WHERE NOT EXISTS (SELECT 1 FROM manual_set_images msi
        WHERE msi.manual_set_version_id = msa.manual_set_version_id
          AND msi.image_id = msa.image_id))
  + (SELECT count(*) FROM manual_set_det_annotations msa
      WHERE NOT EXISTS (SELECT 1 FROM manual_set_images msi
        WHERE msi.manual_set_version_id = msa.manual_set_version_id
          AND msi.image_id = msa.image_id)))

  -- 有做 dedup 的版本不該留下同內容的兩張圖
  UNION ALL SELECT 5, '版本內仍有 blake3 重複', (
    SELECT count(*) FROM (
      SELECT msi.manual_set_version_id, i.blake3_hash
      FROM manual_set_images msi
      JOIN images i ON i.id = msi.image_id
      WHERE i.blake3_hash IS NOT NULL
      GROUP BY 1,2 HAVING count(*) > 1) x)

  -- 宣告了類別卻沒有任何 local category 映射過來 = 死映射
  UNION ALL SELECT 6, '沒有來源的 target category', (
    SELECT count(*) FROM manual_set_target_categories tc
    WHERE NOT EXISTS (SELECT 1 FROM manual_set_category_mappings cm
                      WHERE cm.target_category_id = tc.id))

  -- 這種標註訓練時讀不到 class name，build 應該要擋下來
  UNION ALL SELECT 7, '有標註卻沒有 target 映射', (
    SELECT count(*) FROM manual_set_cls_annotations msa
    JOIN cls_annotations a ON a.id = msa.cls_annotation_id
    WHERE NOT EXISTS (SELECT 1 FROM manual_set_category_mappings cm
                      WHERE cm.manual_set_version_id = msa.manual_set_version_id
                        AND cm.category_id = a.category_id))

  -- 探索過程不該落庫：不存在任何 run / session 紀錄表
  UNION ALL SELECT 8, '不該存在的 run/session 表', (
    SELECT count(*) FROM information_schema.tables
    WHERE table_schema = 'public'
      AND (table_name LIKE '%build_run%' OR table_name LIKE '%build_session%'))

  -- 一個版本剛好一份 spec
  UNION ALL SELECT 9, '版本數與 spec 數不符', (
    SELECT abs((SELECT count(*) FROM manual_set_versions)
             - (SELECT count(*) FROM manual_set_build_specs)))

  -- 每份 spec 都要有步驟（空的 spec 產不出東西）
  UNION ALL SELECT 10, '沒有步驟的 spec', (
    SELECT count(*) FROM manual_set_build_specs
    WHERE coalesce(jsonb_array_length(spec -> 'steps'), 0) = 0)

  -- manual-set 是 training-ready 的：不能有沒標註的影像
  UNION ALL SELECT 11, 'manual-set 裡沒標註的影像', (
    SELECT count(*) FROM manual_set_images msi
    WHERE NOT EXISTS (
        SELECT 1 FROM manual_set_cls_annotations a
        WHERE a.manual_set_version_id = msi.manual_set_version_id
          AND a.image_id = msi.image_id)
      AND NOT EXISTS (
        SELECT 1 FROM manual_set_det_annotations d
        WHERE d.manual_set_version_id = msi.manual_set_version_id
          AND d.image_id = msi.image_id))

  -- 每個版本都該查得到是誰建的
  UNION ALL SELECT 12, '沒有建立者的版本', (
    SELECT count(*) FROM manual_set_build_specs
    WHERE created_by_name = '' OR created_by_email NOT LIKE '%@%')

  -- 溯源只剩 spec 一張表：decisions 與 steps 都改成重跑 spec 得出
  UNION ALL SELECT 13, '不該存在的溯源表', (
    SELECT count(*) FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name IN ('manual_set_build_decisions', 'manual_set_build_steps'))
)
SELECT 檢查項目, 問題數,
       CASE WHEN 問題數 = 0 THEN 'ok' ELSE '<<< 有問題' END AS 結果
FROM checks ORDER BY seq;
