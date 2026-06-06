create table if not exists analysis_runs (
  analysis_id text primary key,
  analysis_time timestamptz,
  completed_at timestamptz,
  symbol text not null,
  mode text,
  price numeric,
  price_date date,
  is_realtime_price boolean,
  market_state text,
  recommendation text,
  direction_score numeric,
  timing_score numeric,
  valuation_score numeric,
  risk_score numeric,
  data_coverage numeric,
  truthfulness_score numeric,
  overall_score numeric,
  analysis_mode text,
  model_used text,
  elapsed_seconds numeric,
  summary_json jsonb,
  scores_json jsonb,
  market_data_json jsonb,
  data_quality_json jsonb,
  truthfulness_json jsonb,
  audit_json jsonb,
  report_markdown text,
  warnings_json jsonb,
  created_at timestamptz default now()
);

alter table analysis_runs add column if not exists audit_json jsonb;

create table if not exists market_snapshots (
  snapshot_id text primary key,
  analysis_time timestamptz,
  symbol text not null,
  price_date date,
  close numeric,
  volume numeric,
  ma20 numeric,
  ma60 numeric,
  scfi_latest numeric,
  scfi_weekly_change numeric,
  freight_trend text,
  institutional_total numeric,
  eps numeric,
  dividend_yield numeric,
  raw_json jsonb,
  created_at timestamptz default now()
);

create table if not exists prediction_validations (
  validation_id text primary key,
  prediction_id text,
  symbol text not null,
  horizon text not null,
  base_price numeric,
  future_price numeric,
  actual_return numeric,
  max_drawdown numeric,
  correct boolean,
  validated_at timestamptz,
  details_json jsonb,
  created_at timestamptz default now()
);

create index if not exists idx_analysis_runs_symbol_time on analysis_runs(symbol, analysis_time desc);
create index if not exists idx_market_snapshots_symbol_date on market_snapshots(symbol, price_date desc);
create index if not exists idx_prediction_validations_symbol on prediction_validations(symbol, horizon);
