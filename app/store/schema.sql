PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA user_version = 1;          -- schema 版本，升级机制见 §3.3

-- ═══════════════════════════════════════════
-- 报告表：一次调研的产物（一行 = 一次调研）
-- ═══════════════════════════════════════════
CREATE TABLE reports (
  id                 TEXT PRIMARY KEY,              -- r-<ulid>
  title              TEXT NOT NULL,                 -- 报告标题
  research_question  TEXT NOT NULL,                 -- 用户原始需求（语义检索的主要输入）
  use_case           TEXT NOT NULL DEFAULT 'other'
                     CHECK (use_case IN ('social_competitor','product_competitor','other')),
                     -- 两个验收用例：社媒竞品 / 竞品产品优缺点
  status             TEXT NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running','completed','failed','archived')),
  created_at         TEXT NOT NULL,
  completed_at       TEXT,
  summary            TEXT,                          -- 执行摘要（结论前置段，供飞书总览与云文档）
  summary_line       TEXT,                          -- 一句话摘要，报告完成时由收尾 agent 生成；
                                                    -- 与 title、report_tags 同为 P3 召回索引的冻结数据源
  plan_snapshot      TEXT CHECK (plan_snapshot IS NULL OR json_valid(plan_snapshot)),
                     -- 最终计划书 JSON 快照：goals、agents、引擎分配、干预点记录
  decision_balance   TEXT CHECK (decision_balance IS NULL OR json_valid(decision_balance)),
                     -- C1 决策天平：动态追问的问答记录（报告内注释的数据源）
  engines_used       TEXT CHECK (engines_used IS NULL OR json_valid(engines_used)),
                     -- ["claude","codex"] 及各自承担的任务类型，供跨报告对比引擎表现
  report_path        TEXT,                          -- 报告正文文件相对路径
  attachments        TEXT CHECK (attachments IS NULL OR json_valid(attachments)),
                     -- [{"type":"excel","path":"...","desc":"..."}]
  feishu_doc_token   TEXT,                          -- 云文档 token（同步后回填）
  feishu_record_id   TEXT,                          -- 多维表格「报告总览」record_id（upsert 锚点）
  feishu_synced_at   TEXT,
  feishu_sync_status TEXT NOT NULL DEFAULT 'pending'
                     CHECK (feishu_sync_status IN ('pending','synced','failed','skipped')),
  extra              TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(extra))
) STRICT;

CREATE INDEX idx_reports_status  ON reports(status);
CREATE INDEX idx_reports_created ON reports(created_at);

-- ═══════════════════════════════════════════
-- 证据表：报告引用的每一条原始信息源记录
-- ═══════════════════════════════════════════
CREATE TABLE evidence (
  id                 TEXT PRIMARY KEY,              -- ev-<ulid>
  report_id          TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  goal_id            TEXT,                          -- 软引用 plan_snapshot 内 goal 编号（如 "goal-1"）
  agent_name         TEXT,                          -- 采集该证据的 agent（如 data-collector）
  engine             TEXT CHECK (engine IN ('claude','codex') OR engine IS NULL),

  -- ── 来源定位（信息源可追溯率 100% 的载体）──
  platform           TEXT NOT NULL,                 -- 'product_hunt'|'hacker_news'|'x'|'reddit'|
                                                    -- 'xhs'|'douyin'|'bilibili'|'baidu_hot'|
                                                    -- 'wechat_mp'|'web_search'|...（可插拔，不用 CHECK 锁死）
  source_type        TEXT NOT NULL DEFAULT 'post'
                     CHECK (source_type IN ('post','comment','video','article',
                                            'search_snippet','ranking_item','profile','other')),
                     -- 'search_snippet' 专为降级后的 Reddit 等索引源设：只有片段+permalink，无全文
  platform_item_id   TEXT,                          -- 平台原生 ID（去重与增量采集用）
  permalink          TEXT NOT NULL,                 -- 原文永久链接（追溯核心，不允许为空）
  title              TEXT,
  content_excerpt    TEXT,                          -- 引用片段/摘要（报告角标悬停展示）
  author_name        TEXT,
  author_meta        TEXT CHECK (author_meta IS NULL OR json_valid(author_meta)),
                     -- 认证状态/粉丝数等，权威性评分的依据
  source_keyword     TEXT,                          -- 这条证据是用什么关键词搜出来的（借鉴 BettaFish）
  fetch_method       TEXT NOT NULL DEFAULT 'official_api'
                     CHECK (fetch_method IN ('official_api','search_index','third_party_api',
                                             'media_crawler','browser_agent','manual')),
                     -- 对应 R3 三档采集分层 + 搜索索引源
  published_at       TEXT,                          -- 原文发布时间（索引源可能拿不到，允许空）
  fetched_at         TEXT NOT NULL,                 -- 抓取时间戳（必填）

  -- ── 热度：R4 跨平台不可直接相加 ──
  raw_metrics        TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(raw_metrics)),
                     -- 平台原始指标原样保留，解析值+原始串双留，例：
                     -- {"votes":1200,"comments":89,"_raw":{"liked_count":"1.2万"}}
  normalized_score   REAL CHECK (normalized_score IS NULL
                                 OR (normalized_score >= 0.0 AND normalized_score <= 1.0)),
                     -- 平台内归一化结果（0–1），只与同平台比较得出
  norm_method        TEXT,                          -- 如 'percentile_in_batch'|'zscore_platform_window'
  norm_context       TEXT CHECK (norm_context IS NULL OR json_valid(norm_context)),
                     -- 归一化参照集描述：{"batch":"goal-1采集批","n":143,"window":"30d"}
                     -- 没有参照集描述的归一化值不可复现，禁止只写分数不写此列

  -- ── 五维可靠度（v1-proposal §③，每维 0–2 分）──
  score_authority    INTEGER CHECK (score_authority    BETWEEN 0 AND 2),
  score_freshness    INTEGER CHECK (score_freshness    BETWEEN 0 AND 2),
  score_crossref     INTEGER CHECK (score_crossref     BETWEEN 0 AND 2),
  score_completeness INTEGER CHECK (score_completeness BETWEEN 0 AND 2),
  score_independence INTEGER CHECK (score_independence BETWEEN 0 AND 2),
  score_total        INTEGER GENERATED ALWAYS AS
                     (score_authority + score_freshness + score_crossref
                      + score_completeness + score_independence) STORED,
  grade              TEXT GENERATED ALWAYS AS (
                       CASE
                         WHEN score_total IS NULL THEN NULL
                         WHEN score_total >= 8 THEN 'A'
                         WHEN score_total >= 6 THEN 'B'
                         WHEN score_total >= 4 THEN 'C'
                         ELSE 'D'
                       END) STORED,                  -- 生成列：等级永不与明细漂移
  rating_notes       TEXT,                          -- 评分理由（角标悬停展开的文案）
  rated_by           TEXT,                          -- 'agent:reliability-auditor' 等

  -- ── 报告内引用 ──
  citation_no        INTEGER,                       -- 报告内角标编号；NULL = 采到但未被引用
  extra              TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(extra)),

  UNIQUE (report_id, permalink)                     -- 同一报告内同一来源只存一行，多处引用共用
) STRICT;

CREATE INDEX idx_evidence_report    ON evidence(report_id);
CREATE INDEX idx_evidence_platform  ON evidence(platform, published_at);
CREATE INDEX idx_evidence_grade     ON evidence(grade);
CREATE INDEX idx_evidence_permalink ON evidence(permalink);

-- ═══════════════════════════════════════════
-- 反馈表：用户对报告的修正、标签调整，及 C1 要求的变更记录
-- ═══════════════════════════════════════════
CREATE TABLE feedback (
  id           TEXT PRIMARY KEY,                    -- fb-<ulid>
  report_id    TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  evidence_id  TEXT REFERENCES evidence(id) ON DELETE SET NULL,
                                                    -- 指向具体证据时填（如改某条可靠度评分）
  kind         TEXT NOT NULL CHECK (kind IN (
                 'content_correction',              -- 用户修正报告内容
                 'tag_adjust',                      -- 用户增删改标签
                 'rating_adjust',                   -- 用户调整证据可靠度评分/等级
                 'goal_change',                     -- C1：运行中 Goal 被调整，原产物不保留但记录变更
                 'note')),                          -- 其他备注类反馈
  target       TEXT,                                -- 指向位置：报告章节锚点 / 字段名 / goal 编号
  before_value TEXT CHECK (before_value IS NULL OR json_valid(before_value)),
  after_value  TEXT CHECK (after_value  IS NULL OR json_valid(after_value)),
                                                    -- 变更前后值都存 JSON，diff 可回放
  reason       TEXT,                                -- 用户/agent 给出的理由
  actor        TEXT NOT NULL DEFAULT 'user',        -- 'user' | 'agent:<name>'
  created_at   TEXT NOT NULL,
  applied      INTEGER NOT NULL DEFAULT 0 CHECK (applied IN (0,1)),
                                                    -- 是否已回写到 reports/evidence/report_tags 现值
  extra        TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(extra))
) STRICT;

CREATE INDEX idx_feedback_report ON feedback(report_id, created_at);

-- ═══════════════════════════════════════════
-- 标签表：报告标签的「现值」（agent 自动打，用户调整后更新）
-- 历史轨迹不在这里——每次调整在 feedback 记 tag_adjust 一条
-- ═══════════════════════════════════════════
CREATE TABLE report_tags (
  report_id  TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  tag        TEXT NOT NULL,                         -- 标签值，建议 agent 用受控词表 + 自由补充
  source     TEXT NOT NULL DEFAULT 'agent' CHECK (source IN ('agent','user')),
  created_at TEXT NOT NULL,
  PRIMARY KEY (report_id, tag)
) STRICT;

CREATE INDEX idx_tags_tag ON report_tags(tag);      -- 跨报告按标签检索

-- ═══════════════════════════════════════════
-- 扩展键登记表：extra JSON 里出现过的键，驱动 §3.3 升级机制
-- 由存储层写入时自动维护，agent 不直接操作
-- ═══════════════════════════════════════════
CREATE TABLE ext_key_registry (
  table_name    TEXT NOT NULL CHECK (table_name IN ('reports','evidence','feedback')),
  key           TEXT NOT NULL,
  value_type    TEXT NOT NULL,                      -- 'text'|'integer'|'real'|'array'|'object'
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  seen_count    INTEGER NOT NULL DEFAULT 1,         -- 出现总次数（行数）
  report_count  INTEGER NOT NULL DEFAULT 1,         -- 覆盖的报告数（升级阈值看这个）
  sample_value  TEXT,                               -- 截断到 200 字符的示例值
  type_conflicts INTEGER NOT NULL DEFAULT 0,        -- 同键不同类型的次数（>0 阻断升级）
  promoted_in   INTEGER,                            -- 升级为正式列时的 user_version；NULL=未升级
  PRIMARY KEY (table_name, key)
) STRICT;

-- ═══════════════════════════════════════════
-- 召回索引：P3 语义检索的 FTS5 粗筛层（BM25），与主表同库同文件
-- trigram 分词中文零依赖（SQLite ≥3.34 内置）；
-- 报告完成或标签变更时由存储层同步 upsert，agent 不直接写
-- ═══════════════════════════════════════════
CREATE VIRTUAL TABLE recall_fts USING fts5(
  report_id UNINDEXED,
  title,
  tags,               -- report_tags 现值以空格拼接
  summary_line,
  tokenize = 'trigram'
);
