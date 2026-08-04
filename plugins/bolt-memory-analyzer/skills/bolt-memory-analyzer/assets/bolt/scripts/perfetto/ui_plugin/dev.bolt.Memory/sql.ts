// Copyright (C) 2026 ByteDance Ltd. and/or its affiliates
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import type {AreaSelection} from '../../public/selection';

export const BOLT_MEMORY_VIEWS_SQL = `
CREATE PERFETTO VIEW bolt_memory_pool AS
SELECT
  CAST(EXTRACT_ARG(s.arg_set_id, 'debug.pool_id') AS INT) AS pool_id,
  EXTRACT_ARG(s.arg_set_id, 'debug.pool') AS pool
FROM slice s
JOIN track t ON s.track_id = t.id
WHERE t.name = 'Bolt MemoryPool metadata'
  AND s.name = 'pool_definition';

CREATE PERFETTO VIEW bolt_memory_event AS
SELECT
  s.id,
  s.ts,
  CAST(EXTRACT_ARG(s.arg_set_id, 'debug.seq') AS INT) AS seq,
  CAST(EXTRACT_ARG(s.arg_set_id, 'debug.allocation_id') AS INT)
      AS allocation_id,
  CAST(EXTRACT_ARG(s.arg_set_id, 'debug.related_allocation_id') AS INT)
      AS related_allocation_id,
  s.name AS op,
  pool.pool,
  CAST(EXTRACT_ARG(s.arg_set_id, 'debug.address') AS INT) AS address,
  CAST(EXTRACT_ARG(s.arg_set_id, 'debug.old_address') AS INT)
      AS old_address,
  CAST(EXTRACT_ARG(s.arg_set_id, 'debug.size') AS INT) AS size,
  CAST(EXTRACT_ARG(s.arg_set_id, 'debug.old_size') AS INT) AS old_size,
  CAST(EXTRACT_ARG(s.arg_set_id, 'debug.pool_id') AS INT) AS pool_id,
  COALESCE(
    CAST(EXTRACT_ARG(s.arg_set_id, 'debug.stack_id') AS INT),
    0
  ) AS stack_id,
  CAST(EXTRACT_ARG(s.arg_set_id, 'debug.tid') AS INT) AS tid
FROM slice s
JOIN track t ON s.track_id = t.id
LEFT JOIN bolt_memory_pool pool
  ON pool.pool_id =
      CAST(EXTRACT_ARG(s.arg_set_id, 'debug.pool_id') AS INT)
WHERE t.name = 'Bolt MemoryPool events';

CREATE PERFETTO VIEW bolt_memory_stack AS
SELECT
  CAST(EXTRACT_ARG(s.arg_set_id, 'debug.stack_id') AS INT) AS stack_id,
  CAST(EXTRACT_ARG(s.arg_set_id, 'debug.leaf_node_id') AS INT)
      AS leaf_node_id
FROM slice s
JOIN track t ON s.track_id = t.id
WHERE t.name = 'Bolt MemoryPool metadata'
  AND s.name = 'stack_definition'
UNION ALL
SELECT 0 AS stack_id, 1 AS leaf_node_id;

CREATE PERFETTO VIEW bolt_memory_stack_node AS
SELECT
  CAST(EXTRACT_ARG(s.arg_set_id, 'debug.node_id') AS INT) AS id,
  CASE
    WHEN CAST(EXTRACT_ARG(s.arg_set_id, 'debug.parent_id') AS INT) = 0
      THEN NULL
    ELSE CAST(EXTRACT_ARG(s.arg_set_id, 'debug.parent_id') AS INT)
  END AS parent_id,
  EXTRACT_ARG(s.arg_set_id, 'debug.name') AS name
FROM slice s
JOIN track t ON s.track_id = t.id
WHERE t.name = 'Bolt MemoryPool metadata'
  AND s.name = 'stack_node_definition';

CREATE PERFETTO VIEW bolt_memory_state_interval AS
SELECT *
FROM (
  SELECT
    allocation_id,
    ts AS start_ts,
    LEAD(ts) OVER (PARTITION BY allocation_id ORDER BY seq) AS end_ts,
    seq,
    op,
    pool,
    stack_id,
    address,
    size
  FROM bolt_memory_event
)
WHERE op IN ('alloc', 'grow');
`;

export function buildAreaMetricsSql(selection: AreaSelection): string {
  return `
    WITH
    bounds AS (
      SELECT
        ${selection.start} AS range_start,
        ${selection.end} AS range_end
    ),
    event_values AS (
      SELECT
        stack_id,
        SUM(
          CASE
            WHEN op = 'alloc' THEN size
            WHEN op = 'grow' THEN MAX(size - old_size, 0)
            ELSE 0
          END
        ) AS allocated_bytes,
        SUM(
          CASE
            WHEN op = 'alloc' THEN 1
            WHEN op = 'grow' AND size > old_size THEN 1
            ELSE 0
          END
        ) AS allocation_count
      FROM bolt_memory_event, bounds
      WHERE ts >= bounds.range_start
        AND ts < bounds.range_end
      GROUP BY stack_id
    ),
    state_values AS (
      SELECT
        stack_id,
        SUM(
          CASE
            WHEN state.start_ts <= bounds.range_end
              AND (state.end_ts IS NULL OR state.end_ts > bounds.range_end)
            THEN size
            ELSE 0
          END
        ) AS live_end_bytes,
        SUM(
          CAST(size AS REAL) *
          MAX(
            0,
            MIN(
              COALESCE(state.end_ts, bounds.range_end),
              bounds.range_end
            ) -
            MAX(state.start_ts, bounds.range_start)
          ) / 1000000000.0
        ) AS byte_seconds
      FROM bolt_memory_state_interval state, bounds
      WHERE state.start_ts < bounds.range_end
        AND (
          state.end_ts IS NULL OR state.end_ts > bounds.range_start
        )
      GROUP BY stack_id
    ),
    stack_ids AS (
      SELECT stack_id FROM event_values
      UNION
      SELECT stack_id FROM state_values
    ),
    stack_values AS (
      SELECT
        stack_ids.stack_id,
        COALESCE(event_values.allocated_bytes, 0) AS allocated_bytes,
        COALESCE(event_values.allocation_count, 0) AS allocation_count,
        COALESCE(state_values.live_end_bytes, 0) AS live_end_bytes,
        COALESCE(state_values.byte_seconds, 0) AS byte_seconds
      FROM stack_ids
      LEFT JOIN event_values USING (stack_id)
      LEFT JOIN state_values USING (stack_id)
    ),
    leaf_values AS (
      SELECT
        stack.leaf_node_id,
        SUM(metrics.allocated_bytes) AS allocated_bytes,
        SUM(metrics.allocation_count) AS allocation_count,
        SUM(metrics.live_end_bytes) AS live_end_bytes,
        SUM(metrics.byte_seconds) AS byte_seconds
      FROM stack_values metrics
      JOIN bolt_memory_stack stack USING (stack_id)
      GROUP BY stack.leaf_node_id
    )
    SELECT
      CAST(node.id AS INT) AS id,
      CAST(node.parent_id AS INT) AS parentId,
      printf('%s', node.name) AS name,
      CAST(COALESCE(leaf.allocated_bytes, 0) AS INT) AS allocated_bytes,
      CAST(COALESCE(leaf.allocation_count, 0) AS INT) AS allocation_count,
      CAST(COALESCE(leaf.live_end_bytes, 0) AS INT) AS live_end_bytes,
      CAST(COALESCE(leaf.byte_seconds, 0) AS REAL) AS byte_seconds
    FROM bolt_memory_stack_node node
    LEFT JOIN leaf_values leaf ON leaf.leaf_node_id = node.id
  `;
}
