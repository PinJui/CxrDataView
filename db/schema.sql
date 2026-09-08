-- =========================================================
-- CXR Dataset Management System — PostgreSQL schema
--
-- Design principles
--   1. Only original-sets own real image/annotation data. image_batches and
--      annotation_batches are versioned independently of each other.
--   2. Manual-sets are fully independent: they copy no image or annotation
--      data, only membership rows recording which images/annotations a given
--      manual-set version selected, referencing original-set data directly.
--   3. Category namespaces are scoped to an annotation_batch. Ids are never
--      shared across versions or datasets — the same name in two batches is
--      deliberately two different categories.
--   4. Category mapping aggregates many local categories into one canonical
--      target category, scoped to a manual_set_version.
--   5. Images may have no annotations (not yet labelled). Categories may NOT
--      be dangling — enforced by a deferred constraint trigger at the bottom.
--   6. When a manual-set selects an annotation, that annotation's image must
--      already be a member of the same manual-set version. Composite foreign
--      keys enforce this declaratively; no trigger needed.
--   7. images.blake3_hash enables content-level duplicate detection.
--   8. image_lineage records derivation (preprocessing, cropping, ...).
--   9. Exploration never touches the database. A build spec is written only
--      at commit time, and one spec maps to exactly one manual_set_version.
--      That pair is the entire record: anything else about how the set was
--      built is derived by re-running the spec.
--
-- Apply with:  cxr db init [--drop]
-- =========================================================

BEGIN;

-- ---------------------------------------------------------
-- original_sets: the dataset itself (versionless)
-- ---------------------------------------------------------
CREATE TABLE original_sets (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------
-- image_batches: original-sets/{name}/images/V{version}/
-- ---------------------------------------------------------
CREATE TABLE image_batches (
    id              BIGSERIAL PRIMARY KEY,
    original_set_id BIGINT NOT NULL REFERENCES original_sets(id) ON DELETE CASCADE,
    version         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (original_set_id, version)
);

-- ---------------------------------------------------------
-- annotation_batches: original-sets/{name}/annotations/V{version}/
-- ---------------------------------------------------------
CREATE TABLE annotation_batches (
    id              BIGSERIAL PRIMARY KEY,
    original_set_id BIGINT NOT NULL REFERENCES original_sets(id) ON DELETE CASCADE,
    version         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (original_set_id, version)
);

-- ---------------------------------------------------------
-- licenses: a global vocabulary, deliberately NOT scoped per original-set.
-- Licence terms (CC BY-SA 4.0 and friends) are fixed, reusable vocabulary and
-- do not suffer the "same name, different meaning" problem categories have.
-- ---------------------------------------------------------
CREATE TABLE licenses (
    id      BIGSERIAL PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,
    url     TEXT
);

-- ---------------------------------------------------------
-- images: image metadata (pixel data lives in object storage).
--
-- blake3_hash    content hash; the same picture is often re-ingested under a
--                different file name or in a different batch.
-- license_id     NULL means unknown/not applicable. More honest than the
--                placeholder string the file format uses; map back on export.
-- date_captured  NULL when unknown, rather than the "0000-00-00" placeholder.
-- ---------------------------------------------------------
CREATE TABLE images (
    id              BIGSERIAL PRIMARY KEY,
    image_batch_id  BIGINT NOT NULL REFERENCES image_batches(id) ON DELETE CASCADE,
    file_name       TEXT NOT NULL,
    height          INTEGER NOT NULL CHECK (height > 0),
    width           INTEGER NOT NULL CHECK (width > 0),
    blake3_hash     CHAR(64) CHECK (blake3_hash ~ '^[0-9a-f]{64}$'),
    license_id      BIGINT REFERENCES licenses(id) ON DELETE RESTRICT,
    date_captured   DATE,
    UNIQUE (image_batch_id, file_name),
    UNIQUE (id, image_batch_id)   -- referenced by composite foreign keys below
);

CREATE INDEX idx_images_image_batch_id ON images (image_batch_id);
CREATE INDEX idx_images_blake3_hash ON images (blake3_hash);
CREATE INDEX idx_images_license_id ON images (license_id);

-- ---------------------------------------------------------
-- image_lineage: parent/child relationships produced by processing steps.
-- The processing itself is documented by the child's image_batch.
-- ---------------------------------------------------------
CREATE TABLE image_lineage (
    parent_image_id BIGINT NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    child_image_id  BIGINT NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    -- Composite PK prevents duplicate edges and indexes parent -> child.
    PRIMARY KEY (parent_image_id, child_image_id)
);

-- Needed separately to walk child -> parent (tracing provenance upwards).
CREATE INDEX idx_image_lineage_child ON image_lineage (child_image_id);

-- ---------------------------------------------------------
-- annotators: global, unlike categories. A person is the same person in every
-- dataset, so keeping them global is what makes cross-batch and cross-dataset
-- workload and consistency statistics possible.
-- ---------------------------------------------------------
CREATE TABLE annotators (
    id      BIGSERIAL PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE
);

-- ---------------------------------------------------------
-- categories: one namespace per annotation_batch.
-- ---------------------------------------------------------
CREATE TABLE categories (
    id                    BIGSERIAL PRIMARY KEY,
    annotation_batch_id   BIGINT NOT NULL REFERENCES annotation_batches(id) ON DELETE CASCADE,
    name                  TEXT NOT NULL,
    supercategory         TEXT,
    UNIQUE (annotation_batch_id, id),
    UNIQUE (annotation_batch_id, name)
);

-- ---------------------------------------------------------
-- cls_annotations: real classification labels owned by an original-set.
-- UNIQUE(id, image_id) is what lets manual_set_cls_annotations prove, via a
-- composite foreign key, that it did not misattribute an annotation.
-- ---------------------------------------------------------
CREATE TABLE cls_annotations (
    id                   BIGSERIAL PRIMARY KEY,
    annotation_batch_id  BIGINT NOT NULL REFERENCES annotation_batches(id) ON DELETE CASCADE,
    image_id             BIGINT NOT NULL REFERENCES images(id) ON DELETE RESTRICT,
    category_id          BIGINT NOT NULL,
    score                NUMERIC NOT NULL DEFAULT 0,
    annotator_id         BIGINT NOT NULL REFERENCES annotators(id) ON DELETE RESTRICT,
    FOREIGN KEY (annotation_batch_id, category_id)
        REFERENCES categories (annotation_batch_id, id) ON DELETE CASCADE,
    UNIQUE (id, image_id)
);

CREATE INDEX idx_cls_annotations_batch ON cls_annotations (annotation_batch_id);
CREATE INDEX idx_cls_annotations_image ON cls_annotations (image_id);
CREATE INDEX idx_cls_annotations_category ON cls_annotations (category_id);
CREATE INDEX idx_cls_annotations_annotator ON cls_annotations (annotator_id);

-- ---------------------------------------------------------
-- det_annotations: real detection labels owned by an original-set.
-- ---------------------------------------------------------
CREATE TABLE det_annotations (
    id                   BIGSERIAL PRIMARY KEY,
    annotation_batch_id  BIGINT NOT NULL REFERENCES annotation_batches(id) ON DELETE CASCADE,
    image_id             BIGINT NOT NULL REFERENCES images(id) ON DELETE RESTRICT,
    category_id          BIGINT NOT NULL,
    bbox                 NUMERIC[] NOT NULL CHECK (array_length(bbox, 1) = 4),
    segmentation         JSONB,
    iscrowd              SMALLINT NOT NULL DEFAULT 0 CHECK (iscrowd IN (0, 1)),
    score                NUMERIC,
    annotator_id         BIGINT NOT NULL REFERENCES annotators(id) ON DELETE RESTRICT,
    FOREIGN KEY (annotation_batch_id, category_id)
        REFERENCES categories (annotation_batch_id, id) ON DELETE CASCADE,
    UNIQUE (id, image_id)
);

CREATE INDEX idx_det_annotations_batch ON det_annotations (annotation_batch_id);
CREATE INDEX idx_det_annotations_image ON det_annotations (image_id);
CREATE INDEX idx_det_annotations_category ON det_annotations (category_id);
CREATE INDEX idx_det_annotations_annotator ON det_annotations (annotator_id);

-- ---------------------------------------------------------
-- image_subjects: patient / subject identity.
--
-- Deliberately global and not scoped to an image_batch: the same patient may
-- appear in several batches and several original-sets, and only a global key
-- lets the "never split a patient across train and val" guarantee extend to
-- combinations drawn from multiple sources.
--
-- Images without a row here are treated as independent during sampling
-- (image_id is used as the fallback key) and the build reports how many were
-- handled that way, so nobody mistakes a partial guarantee for a full one.
-- ---------------------------------------------------------
CREATE TABLE image_subjects (
    image_id    BIGINT PRIMARY KEY REFERENCES images(id) ON DELETE CASCADE,
    subject_id  TEXT NOT NULL
);

CREATE INDEX idx_image_subjects_subject ON image_subjects (subject_id);

-- =========================================================
-- Manual-sets: selection only. No image or annotation data is ever copied.
-- =========================================================

CREATE TABLE manual_sets (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------
-- manual_set_versions: manual-sets/{name}/annotations/V{version}/
-- A manual-set has a single version axis, unlike an original-set, because it
-- holds no image data of its own — only a selection.
-- ---------------------------------------------------------
CREATE TABLE manual_set_versions (
    id              BIGSERIAL PRIMARY KEY,
    manual_set_id   BIGINT NOT NULL REFERENCES manual_sets(id) ON DELETE CASCADE,
    version         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (manual_set_id, version)
);

-- ---------------------------------------------------------
-- manual_set_images: which images this version selected.
--
-- Every image here must carry at least one annotation in the same version —
-- see the trigger at the bottom of this file. An original-set image is allowed
-- to be "not yet labelled", but a manual-set is a training-ready dataset, and
-- an unlabelled image in it is either a mistake or dead weight.
-- ---------------------------------------------------------
CREATE TABLE manual_set_images (
    manual_set_version_id  BIGINT NOT NULL REFERENCES manual_set_versions(id) ON DELETE CASCADE,
    image_id               BIGINT NOT NULL REFERENCES images(id) ON DELETE RESTRICT,
    PRIMARY KEY (manual_set_version_id, image_id)
);

CREATE INDEX idx_manual_set_images_image ON manual_set_images (image_id);

-- ---------------------------------------------------------
-- manual_set_cls_annotations: which classification labels this version chose.
--
-- The two composite foreign keys together guarantee:
--   (a) image_id really is the annotation's own image — it cannot be forged;
--   (b) that (version, image) pair is already in manual_set_images, i.e. an
--       annotation can only be selected once its image has been selected.
-- Both are rejected at write time; no trigger required.
-- ---------------------------------------------------------
CREATE TABLE manual_set_cls_annotations (
    manual_set_version_id  BIGINT NOT NULL REFERENCES manual_set_versions(id) ON DELETE CASCADE,
    cls_annotation_id      BIGINT NOT NULL,
    image_id               BIGINT NOT NULL,
    PRIMARY KEY (manual_set_version_id, cls_annotation_id),

    FOREIGN KEY (cls_annotation_id, image_id)
        REFERENCES cls_annotations (id, image_id),

    FOREIGN KEY (manual_set_version_id, image_id)
        REFERENCES manual_set_images (manual_set_version_id, image_id)
);

CREATE INDEX idx_manual_set_cls_ann_image
    ON manual_set_cls_annotations (manual_set_version_id, image_id);

-- ---------------------------------------------------------
-- manual_set_det_annotations: same, for detection labels.
-- ---------------------------------------------------------
CREATE TABLE manual_set_det_annotations (
    manual_set_version_id  BIGINT NOT NULL REFERENCES manual_set_versions(id) ON DELETE CASCADE,
    det_annotation_id      BIGINT NOT NULL,
    image_id               BIGINT NOT NULL,
    PRIMARY KEY (manual_set_version_id, det_annotation_id),

    FOREIGN KEY (det_annotation_id, image_id)
        REFERENCES det_annotations (id, image_id),

    FOREIGN KEY (manual_set_version_id, image_id)
        REFERENCES manual_set_images (manual_set_version_id, image_id)
);

CREATE INDEX idx_manual_set_det_ann_image
    ON manual_set_det_annotations (manual_set_version_id, image_id);

-- ---------------------------------------------------------
-- manual_set_target_categories: the training-ready label vocabulary this
-- version defines for itself. Each version owns its own copy.
-- ---------------------------------------------------------
CREATE TABLE manual_set_target_categories (
    id                      BIGSERIAL PRIMARY KEY,
    manual_set_version_id   BIGINT NOT NULL REFERENCES manual_set_versions(id) ON DELETE CASCADE,
    name                    TEXT NOT NULL,
    UNIQUE (manual_set_version_id, name),
    UNIQUE (id, manual_set_version_id)  -- keeps the FK below version-scoped
);

-- ---------------------------------------------------------
-- manual_set_category_mappings: which local category (scoped to an
-- annotation_batch) becomes which target category, in this version only.
-- ---------------------------------------------------------
CREATE TABLE manual_set_category_mappings (
    manual_set_version_id   BIGINT NOT NULL,
    category_id             BIGINT NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    target_category_id      BIGINT NOT NULL,
    PRIMARY KEY (manual_set_version_id, category_id),
    FOREIGN KEY (manual_set_version_id)
        REFERENCES manual_set_versions(id) ON DELETE CASCADE,
    FOREIGN KEY (target_category_id, manual_set_version_id)
        REFERENCES manual_set_target_categories(id, manual_set_version_id) ON DELETE CASCADE
);

-- =========================================================
-- Build specs and provenance
-- =========================================================

-- ---------------------------------------------------------
-- manual_set_import_lists: the body of an externally supplied file-name list.
-- A spec stores only the sha256, keeping the spec JSON readable; this table
-- deduplicates naturally when several manual-sets use the same list.
-- ---------------------------------------------------------
CREATE TABLE manual_set_import_lists (
    sha256      CHAR(64) PRIMARY KEY,
    file_names  TEXT[] NOT NULL,
    source_note TEXT,                -- human-readable note, not a file locator
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------
-- manual_set_build_specs: the spec that produced a version, stored verbatim
-- as JSONB so it can be diffed, reviewed and re-executed.
--
-- One spec per version (UNIQUE). Versions are immutable — you make a new
-- version rather than editing one — so there is no notion of "the same spec
-- was run five times" and no separate run table.
--
-- This is the whole provenance model: a version, and the spec that produced it.
-- Per-step results were stored here once and removed: two of their columns were
-- verbatim copies of the spec, the step count is `jsonb_array_length(spec ->
-- 'steps')`, and the statistics nothing ever read. "What did step 5 do" is a
-- function of (spec, data), and `cxr why` re-runs the spec to answer it.
-- ---------------------------------------------------------
CREATE TABLE manual_set_build_specs (
    id                     BIGSERIAL PRIMARY KEY,
    manual_set_version_id  BIGINT NOT NULL UNIQUE
        REFERENCES manual_set_versions(id) ON DELETE CASCADE,
    spec                   JSONB NOT NULL,
    spec_sha256            CHAR(64) NOT NULL,
    -- Who built this dataset. Deliberately NOT a reference to annotators:
    -- that table records who labelled images, and a data engineer who never
    -- labelled anything does not belong in it. Stored as plain text because
    -- there is no user account system and this is an internal tool.
    created_by_name        TEXT NOT NULL,
    created_by_email       TEXT NOT NULL CHECK (created_by_email LIKE '%@%'),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_build_specs_sha ON manual_set_build_specs (spec_sha256);

-- =========================================================
-- Views
-- =========================================================

-- Lets a spec locate a batch by (original_set_name, version) so nobody ever
-- has to type a bigint id.
CREATE VIEW v_batches AS
SELECT os.name AS original_set_name, 'image' AS batch_kind,
       ib.version, ib.id AS batch_id, ib.created_at
FROM image_batches ib JOIN original_sets os ON os.id = ib.original_set_id
UNION ALL
SELECT os.name, 'annotation', ab.version, ab.id, ab.created_at
FROM annotation_batches ab JOIN original_sets os ON os.id = ab.original_set_id;

-- =========================================================
-- Constraint trigger: categories must not dangle
--
-- Rule: every surviving category must be referenced by at least one row in
-- cls_annotations or det_annotations.
--
-- DEFERRABLE INITIALLY DEFERRED so the check runs just before COMMIT, which
-- permits the legitimate ordering "create the category, then insert the
-- annotations that reference it" inside one transaction.
--
-- Retiring a category (delete its annotations, then delete the category) is
-- handled: a category row that no longer exists is skipped rather than
-- reported.
--
-- Note that images are NOT required to have annotations. That restriction only
-- made sense when images.parquet and annotations.parquet were written
-- separately; here an image is allowed to be "not yet labelled".
-- =========================================================

CREATE OR REPLACE FUNCTION check_category_not_dangling()
RETURNS trigger AS $$
DECLARE
    affected_ids BIGINT[] := ARRAY[]::BIGINT[];
    cid          BIGINT;
    cat_exists   BOOLEAN;
    has_ref      BOOLEAN;
BEGIN
    IF TG_TABLE_NAME = 'categories' THEN
        affected_ids := ARRAY[NEW.id];
    ELSE
        IF TG_OP IN ('DELETE', 'UPDATE') THEN
            affected_ids := affected_ids || OLD.category_id;
        END IF;
        IF TG_OP IN ('INSERT', 'UPDATE') THEN
            affected_ids := affected_ids || NEW.category_id;
        END IF;
    END IF;

    FOREACH cid IN ARRAY affected_ids LOOP
        SELECT EXISTS (SELECT 1 FROM categories WHERE id = cid) INTO cat_exists;
        IF NOT cat_exists THEN
            CONTINUE; -- already deleted; nothing to dangle
        END IF;

        SELECT EXISTS (
            SELECT 1 FROM cls_annotations WHERE category_id = cid
            UNION ALL
            SELECT 1 FROM det_annotations WHERE category_id = cid
        ) INTO has_ref;

        IF NOT has_ref THEN
            RAISE EXCEPTION
                'Dangling category: category_id=% has no referencing row in cls_annotations or det_annotations',
                cid;
        END IF;
    END LOOP;

    RETURN NULL; -- AFTER trigger; return value ignored
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_categories_not_dangling
    AFTER INSERT OR UPDATE ON categories
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION check_category_not_dangling();

CREATE CONSTRAINT TRIGGER trg_cls_annotations_category_check
    AFTER INSERT OR UPDATE OR DELETE ON cls_annotations
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION check_category_not_dangling();

CREATE CONSTRAINT TRIGGER trg_det_annotations_category_check
    AFTER INSERT OR UPDATE OR DELETE ON det_annotations
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION check_category_not_dangling();

-- =========================================================
-- Constraint trigger: a manual-set image must be annotated
--
-- Rule: every row in manual_set_images must have at least one matching row in
-- manual_set_cls_annotations or manual_set_det_annotations, in the same
-- version. A manual-set is a training-ready dataset; an image in it with no
-- label is either an oversight or dead weight, and finding out at training
-- time is far too late.
--
-- DEFERRABLE INITIALLY DEFERRED because a build necessarily writes the images
-- first and the annotations after — the composite foreign keys require exactly
-- that order — so the check can only be meaningful at COMMIT.
--
-- Deleting a whole version cascades to both tables; a manual_set_images row
-- that is itself gone is skipped rather than reported.
-- =========================================================

CREATE OR REPLACE FUNCTION check_manual_set_image_is_annotated()
RETURNS trigger AS $$
DECLARE
    vid       BIGINT;
    iid       BIGINT;
    still_in  BOOLEAN;
    has_ann   BOOLEAN;
BEGIN
    IF TG_OP = 'DELETE' THEN
        vid := OLD.manual_set_version_id;
        iid := OLD.image_id;
    ELSE
        vid := NEW.manual_set_version_id;
        iid := NEW.image_id;
    END IF;

    SELECT EXISTS (
        SELECT 1 FROM manual_set_images
        WHERE manual_set_version_id = vid AND image_id = iid
    ) INTO still_in;
    IF NOT still_in THEN
        RETURN NULL;  -- the image left the set; nothing to require
    END IF;

    SELECT EXISTS (
        SELECT 1 FROM manual_set_cls_annotations
        WHERE manual_set_version_id = vid AND image_id = iid
        UNION ALL
        SELECT 1 FROM manual_set_det_annotations
        WHERE manual_set_version_id = vid AND image_id = iid
    ) INTO has_ann;

    IF NOT has_ann THEN
        RAISE EXCEPTION
            'Unannotated image in manual-set: version_id=%, image_id=% has no '
            'cls or det annotation selected. A manual-set is training-ready; '
            'either select an annotation for it or drop the image.',
            vid, iid;
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_manual_set_image_annotated
    AFTER INSERT OR UPDATE ON manual_set_images
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION check_manual_set_image_is_annotated();

CREATE CONSTRAINT TRIGGER trg_manual_set_cls_ann_keeps_image_annotated
    AFTER DELETE ON manual_set_cls_annotations
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION check_manual_set_image_is_annotated();

CREATE CONSTRAINT TRIGGER trg_manual_set_det_ann_keeps_image_annotated
    AFTER DELETE ON manual_set_det_annotations
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION check_manual_set_image_is_annotated();

COMMIT;
