-- DuckDB schema. Re-runnable: every write is an upsert on the natural key.
-- No author logins are stored: the digest never needs to know who filed what.

CREATE TABLE IF NOT EXISTS issue (
  repo               VARCHAR     NOT NULL,
  number             INTEGER     NOT NULL,
  title              VARCHAR     NOT NULL,
  body               VARCHAR,
  content_sha        VARCHAR     NOT NULL,  -- sha256(title \n body): re-triage only when this changes
  state              VARCHAR     NOT NULL,
  state_reason       VARCHAR,
  labels             VARCHAR[]   NOT NULL,
  author_association VARCHAR,               -- OWNER/MEMBER/COLLABORATOR = staff-filed, excluded from user signal
  comments           INTEGER     NOT NULL,
  reactions          INTEGER     NOT NULL,
  html_url           VARCHAR     NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL,
  updated_at         TIMESTAMPTZ NOT NULL,
  closed_at          TIMESTAMPTZ,
  fetched_at         TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (repo, number)
);

CREATE TABLE IF NOT EXISTS cohort (
  repo            VARCHAR NOT NULL,
  number          INTEGER NOT NULL,
  form_kind       VARCHAR NOT NULL,
  platform        VARCHAR NOT NULL,
  platform_source VARCHAR NOT NULL,
  os              VARCHAR NOT NULL,
  terminal        VARCHAR NOT NULL,
  model           VARCHAR NOT NULL,
  version         VARCHAR,
  regression      VARCHAR,
  priority        VARCHAR,
  impact          VARCHAR,
  PRIMARY KEY (repo, number)
);

CREATE TABLE IF NOT EXISTS changelog_entry (
  repo        VARCHAR NOT NULL,
  version     VARCHAR NOT NULL,
  position    INTEGER NOT NULL,
  change_type VARCHAR NOT NULL,
  surface     VARCHAR,
  text        VARCHAR NOT NULL,
  PRIMARY KEY (repo, version, position)
);

CREATE TABLE IF NOT EXISTS release (
  repo        VARCHAR     NOT NULL,
  version     VARCHAR     NOT NULL,
  released_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (repo, version)
);

-- One row per (issue content, prompt version, model, effort): a new prompt adds rows, never overwrites,
-- so prompt versions can be compared on the same issues.
CREATE TABLE IF NOT EXISTS triage (
  repo             VARCHAR     NOT NULL,
  number           INTEGER     NOT NULL,
  content_sha      VARCHAR     NOT NULL,
  prompt_version   VARCHAR     NOT NULL,
  model            VARCHAR     NOT NULL,
  effort           VARCHAR     NOT NULL,
  kind             VARCHAR,
  area             VARCHAR,
  proposed_area    VARCHAR,
  ask              VARCHAR,
  blocked          BOOLEAN,
  blocked_reason   VARCHAR,
  severity         VARCHAR,
  evidence_quote   VARCHAR,
  evidence_valid   BOOLEAN,
  confidence       VARCHAR,
  lines_omitted    INTEGER     NOT NULL,
  status           VARCHAR     NOT NULL,  -- auto | needs_review
  review_reason    VARCHAR,
  stop_reason      VARCHAR,
  refusal_category VARCHAR,
  served_model     VARCHAR,
  created_at       TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (repo, number, content_sha, prompt_version, model, effort)
);

CREATE TABLE IF NOT EXISTS llm_call (
  run_id             VARCHAR     NOT NULL,
  purpose            VARCHAR     NOT NULL,  -- triage | cluster
  subject            VARCHAR,               -- issue number or cluster group
  model              VARCHAR     NOT NULL,
  request_id         VARCHAR,
  input_tokens       BIGINT,
  output_tokens      BIGINT,
  cache_read_tokens  BIGINT,
  cache_write_tokens BIGINT,
  latency_ms         BIGINT,
  cost_usd           DOUBLE,
  stop_reason        VARCHAR,
  error              VARCHAR,
  called_at          TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS cluster (
  repo         VARCHAR   NOT NULL,
  window_end   DATE      NOT NULL,
  grp          VARCHAR   NOT NULL,  -- area/kind group the clustering ran over
  cluster_id   INTEGER   NOT NULL,
  label        VARCHAR   NOT NULL,
  numbers      INTEGER[] NOT NULL,
  PRIMARY KEY (repo, window_end, grp, cluster_id)
);
