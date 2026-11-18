-- Claude Code のイベントを人 × 日（UTC）× 区分に集計する。本文・引数・パス・費用は参照しない。
WITH scoped AS (
  SELECT
    DATE(timestamp, 'UTC') AS date,
    REGEXP_REPLACE(IFNULL(labels.event_name, ''), r'^claude_code\.', '') AS raw_event,
    LOWER(TRIM(IFNULL(labels.user_email, ''))) AS email,
    LOWER(IFNULL(labels.organization_id, '')) AS org_id,
    labels.session_id AS session_id,
    {{terminal_type}} AS terminal_type,
    {{model}} AS model,
    {{query_source}} AS query_source,
    {{effort}} AS effort,
    {{agent_name}} AS agent_name,
    {{skill_name}} AS skill_name,
    {{mcp_server_name}} AS mcp_server_name,
    {{plugin_name}} AS plugin_name,
    {{tool_name}} AS tool_name,
    {{tool_source}} AS tool_source,
    {{decision}} AS decision,
    {{source}} AS source,
    {{permission_mode}} AS permission_mode,
    {{command_source}} AS command_source,
    {{input_tokens}} AS input_tokens,
    {{output_tokens}} AS output_tokens,
    {{cache_read_tokens}} AS cache_read_tokens,
    {{cache_creation_tokens}} AS cache_creation_tokens
  FROM `{{table}}`
  WHERE timestamp >= @start AND timestamp < @end
),
kept AS (
  SELECT
    * EXCEPT (raw_event, org_id),
    IF(REGEXP_CONTAINS(raw_event, r'^[a-z0-9_]{1,64}$'), raw_event, 'other') AS name
  FROM scoped
  WHERE org_id IN UNNEST(SPLIT(@org_ids, ',')) AND email != ''
),
folded AS (
  SELECT
    date, email, session_id, name,
    IF(IFNULL(terminal_type, '') = 'non-interactive', 'non_interactive', 'interactive') AS terminal,
    IF(name = 'api_request',
       IF(REGEXP_CONTAINS(IFNULL(model, ''), r'^[A-Za-z0-9._:\[\]-]{1,100}$'), model, 'other'), NULL) AS model,
    IF(name = 'api_request', CASE
       WHEN query_source = 'repl_main_thread' THEN 'main'
       WHEN query_source = 'sdk' THEN 'sdk'
       WHEN STARTS_WITH(query_source, 'agent:builtin:') THEN 'subagent_builtin'
       WHEN STARTS_WITH(query_source, 'agent:') THEN 'subagent_custom'
       ELSE 'other' END, NULL) AS origin,
    IF(name = 'api_request', CASE
       WHEN effort IS NULL THEN 'none'
       WHEN effort IN ('low', 'medium', 'high', 'xhigh', 'max') THEN effort
       ELSE 'other' END, NULL) AS effort,
    IF(name = 'api_request', agent_name IS NOT NULL, NULL) AS agent,
    IF(name = 'api_request', skill_name IS NOT NULL, NULL) AS skill,
    IF(name = 'api_request', mcp_server_name IS NOT NULL, NULL) AS mcp,
    IF(name = 'api_request', plugin_name IS NOT NULL, NULL) AS plugin,
    -- mcp: 出どころが mcp。出どころの属性が無いイベントでは、伏せた名前（mcp_tool）か mcp__ で始まる名前
    IF(name = 'tool_decision', CASE
       WHEN tool_source = 'mcp' THEN 'mcp'
       WHEN tool_source IS NULL AND (tool_name = 'mcp_tool' OR STARTS_WITH(IFNULL(tool_name, ''), 'mcp__')) THEN 'mcp'
       WHEN tool_name IN ('Edit', 'Write', 'MultiEdit', 'NotebookEdit') THEN 'edit'
       WHEN tool_name IN ('Bash', 'PowerShell') THEN 'shell'
       WHEN tool_name IN ('Agent', 'Task') THEN 'agent'
       WHEN tool_name = 'Skill' THEN 'skill'
       ELSE 'other' END, NULL) AS tool,
    IF(name = 'tool_decision', IF(IFNULL(decision, '') IN ('accept', 'reject'), decision, 'other'), NULL) AS decision,
    IF(name = 'tool_decision',
       IF(IFNULL(source, '') IN ('config', 'hook', 'user_permanent', 'user_temporary', 'user_abort', 'user_reject'),
          source, 'other'),
       NULL) AS source,
    IF(name = 'tool_decision', CASE
       WHEN permission_mode IS NULL THEN 'none'
       WHEN permission_mode IN ('default', 'plan', 'acceptEdits', 'bypassPermissions', 'dontAsk', 'auto')
         THEN permission_mode
       ELSE 'other' END, NULL) AS permission_mode,
    IF(name = 'user_prompt', CASE
       WHEN command_source IS NULL THEN 'none'
       WHEN command_source IN ('builtin', 'custom', 'mcp') THEN command_source
       ELSE 'other' END, NULL) AS command_source,
    IF(name = 'api_request', SAFE_CAST(input_tokens AS INT64), NULL) AS input_tokens,
    IF(name = 'api_request', SAFE_CAST(output_tokens AS INT64), NULL) AS output_tokens,
    IF(name = 'api_request', SAFE_CAST(cache_read_tokens AS INT64), NULL) AS cache_read_tokens,
    IF(name = 'api_request', SAFE_CAST(cache_creation_tokens AS INT64), NULL) AS cache_creation_tokens
  FROM kept
),
event_rows AS (
  SELECT
    'event' AS kind, date, email, name, terminal, model, origin, effort, agent, skill, mcp, plugin,
    tool, decision, source, permission_mode, command_source,
    COUNT(*) AS count,
    SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
    SUM(cache_read_tokens) AS cache_read_tokens, SUM(cache_creation_tokens) AS cache_creation_tokens
  FROM folded
  GROUP BY date, email, name, terminal, model, origin, effort, agent, skill, mcp, plugin,
    tool, decision, source, permission_mode, command_source
),
-- セッションは人ごとに数える。その月に api_request のあったものを、月の中で最初の api_request の日に数える。
-- 非対話の api_request が 1 件でもあれば non_interactive
session_first AS (
  SELECT
    email, session_id, MIN(date) AS date,
    IF(LOGICAL_OR(terminal = 'non_interactive'), 'non_interactive', 'interactive') AS terminal
  FROM folded
  WHERE name = 'api_request' AND session_id IS NOT NULL AND session_id != ''
  GROUP BY email, session_id
),
session_rows AS (
  SELECT date, email, terminal, COUNT(*) AS count FROM session_first GROUP BY date, email, terminal
),
excluded_rows AS (
  SELECT
    CASE WHEN org_id = '' THEN 'excluded_organization_empty'
         WHEN org_id NOT IN UNNEST(SPLIT(@org_ids, ',')) THEN 'excluded_organization_other'
         ELSE 'excluded_email_empty' END AS kind,
    COUNT(*) AS count
  FROM scoped
  WHERE NOT (org_id IN UNNEST(SPLIT(@org_ids, ',')) AND email != '')
  GROUP BY kind
),
-- 月の範囲にあったイベントの総数。必ず 1 行を返す（結果が 0 行にならないようにし、イベントの行の件数の和と
-- 集計しなかった件数の和が、この数に一致することを受け取った側で確かめる）
scanned_rows AS (
  SELECT COUNT(*) AS count FROM scoped
),
unioned AS (
  SELECT kind, date, email, name, terminal, model, origin, effort, agent, skill, mcp, plugin,
    tool, decision, source, permission_mode, command_source, count,
    input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens
  FROM event_rows
  UNION ALL
  SELECT 'session', date, email, CAST(NULL AS STRING), terminal, CAST(NULL AS STRING), CAST(NULL AS STRING),
    CAST(NULL AS STRING), CAST(NULL AS BOOL), CAST(NULL AS BOOL), CAST(NULL AS BOOL), CAST(NULL AS BOOL),
    CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING), count,
    CAST(NULL AS INT64), CAST(NULL AS INT64), CAST(NULL AS INT64), CAST(NULL AS INT64)
  FROM session_rows
  UNION ALL
  SELECT kind, CAST(NULL AS DATE), CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING),
    CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS BOOL), CAST(NULL AS BOOL),
    CAST(NULL AS BOOL), CAST(NULL AS BOOL), CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING),
    CAST(NULL AS STRING), CAST(NULL AS STRING), count,
    CAST(NULL AS INT64), CAST(NULL AS INT64), CAST(NULL AS INT64), CAST(NULL AS INT64)
  FROM excluded_rows
  UNION ALL
  SELECT 'scanned', CAST(NULL AS DATE), CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING),
    CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS BOOL), CAST(NULL AS BOOL),
    CAST(NULL AS BOOL), CAST(NULL AS BOOL), CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING),
    CAST(NULL AS STRING), CAST(NULL AS STRING), count,
    CAST(NULL AS INT64), CAST(NULL AS INT64), CAST(NULL AS INT64), CAST(NULL AS INT64)
  FROM scanned_rows
)
SELECT *, COUNT(*) OVER () AS total_rows
FROM unioned
ORDER BY kind, date, email, name, terminal, model, origin, effort, agent, skill, mcp, plugin,
  tool, decision, source, permission_mode, command_source
