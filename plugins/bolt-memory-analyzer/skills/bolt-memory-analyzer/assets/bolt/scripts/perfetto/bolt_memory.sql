-- Bolt MemoryPool trace views for Perfetto Trace Processor.

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
  CAST(EXTRACT_ARG(s.arg_set_id, 'debug.stack_id') AS INT) AS stack_id,
  CAST(EXTRACT_ARG(s.arg_set_id, 'debug.tid') AS INT) AS tid
FROM slice s
JOIN track t ON s.track_id = t.id
LEFT JOIN (
  SELECT
    CAST(EXTRACT_ARG(pool_slice.arg_set_id, 'debug.pool_id') AS INT)
        AS pool_id,
    EXTRACT_ARG(pool_slice.arg_set_id, 'debug.pool') AS pool
  FROM slice pool_slice
  JOIN track pool_track ON pool_slice.track_id = pool_track.id
  WHERE pool_track.name = 'Bolt MemoryPool metadata'
    AND pool_slice.name = 'pool_definition'
) pool
ON pool.pool_id = CAST(EXTRACT_ARG(s.arg_set_id, 'debug.pool_id') AS INT)
WHERE t.name = 'Bolt MemoryPool events';

CREATE PERFETTO VIEW bolt_memory_stack AS
SELECT
  CAST(EXTRACT_ARG(s.arg_set_id, 'debug.stack_id') AS INT) AS stack_id,
  CAST(EXTRACT_ARG(s.arg_set_id, 'debug.leaf_node_id') AS INT)
      AS leaf_node_id,
  EXTRACT_ARG(s.arg_set_id, 'debug.stack') AS stack
FROM slice s
JOIN track t ON s.track_id = t.id
WHERE t.name = 'Bolt MemoryPool metadata'
  AND s.name = 'stack_definition';

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

CREATE PERFETTO VIEW bolt_memory_allocation_begin AS
SELECT
  allocation_id,
  MIN(ts) AS start_ts,
  MIN(seq) AS start_seq,
  MIN(pool) AS initial_pool,
  MIN(stack_id) AS initial_stack_id
FROM bolt_memory_event
WHERE op IN ('alloc', 'grow')
GROUP BY allocation_id;

CREATE PERFETTO VIEW bolt_memory_allocation_end AS
SELECT
  allocation_id,
  MIN(ts) AS end_ts,
  MIN(seq) AS end_seq
FROM bolt_memory_event
WHERE op = 'free'
GROUP BY allocation_id;

CREATE PERFETTO VIEW bolt_memory_last_state AS
SELECT *
FROM (
  SELECT
    allocation_id,
    ts,
    seq,
    pool,
    stack_id,
    address,
    size,
    ROW_NUMBER() OVER (
      PARTITION BY allocation_id ORDER BY seq DESC
    ) AS row_number
  FROM bolt_memory_event
  WHERE op IN ('alloc', 'grow')
)
WHERE row_number = 1;

CREATE PERFETTO VIEW bolt_memory_lifetime AS
SELECT
  begin.allocation_id,
  begin.start_ts,
  end.end_ts,
  COALESCE(end.end_ts, trace_end()) - begin.start_ts AS lifetime_ns,
  begin.start_seq,
  end.end_seq,
  state.pool,
  state.stack_id,
  state.address,
  state.size,
  end.end_ts IS NULL AS live_at_end
FROM bolt_memory_allocation_begin begin
JOIN bolt_memory_last_state state USING (allocation_id)
LEFT JOIN bolt_memory_allocation_end end USING (allocation_id);

CREATE PERFETTO VIEW bolt_memory_counter AS
SELECT
  counter.ts,
  counter_track.name,
  counter.value
FROM counter
JOIN counter_track ON counter.track_id = counter_track.id
WHERE counter_track.name IN (
  'Bolt active bytes',
  'Bolt active allocations'
);
