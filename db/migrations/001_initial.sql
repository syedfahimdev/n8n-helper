-- Single-row table holding the master resume's extracted text.
CREATE TABLE IF NOT EXISTS master_resume (
  id          int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  text        text NOT NULL,
  pdf_path    text NOT NULL DEFAULT 'resumes/master.pdf',
  uploaded_at timestamptz NOT NULL DEFAULT now()
);

-- Pending tailor offers — pre-approved jobs awaiting the user's button tap.
CREATE TABLE IF NOT EXISTS pending_tailor_offers (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  chat_id        text NOT NULL,
  url            text NOT NULL,
  score          real NOT NULL,
  missing_terms  text[] NOT NULL DEFAULT '{}',
  created_at     timestamptz NOT NULL DEFAULT now(),
  expires_at     timestamptz NOT NULL DEFAULT (now() + interval '1 hour'),
  status         text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','approved','expired'))
);

CREATE INDEX IF NOT EXISTS idx_pending_offers_chat_recent
  ON pending_tailor_offers (chat_id, created_at DESC);

-- NOTE: pg_cron cleanup is omitted because Supabase free tier does not
-- expose pg_cron. The pending_tailor_offers table is read with an
-- `expires_at < now()` check on every access (soft-expire), and an n8n
-- cron task will DELETE long-expired rows daily. See spec §7.
