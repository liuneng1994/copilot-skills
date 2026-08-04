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

import m from 'mithril';
import {FlamegraphPanel} from '../../components/flamegraph_panel';
import {
  metricsFromTableOrSubquery,
  type QueryFlamegraphMetric,
} from '../../components/query_flamegraph';
import type {PerfettoPlugin} from '../../public/plugin';
import type {AreaSelection, AreaSelectionTab} from '../../public/selection';
import type {Trace} from '../../public/trace';
import {NUM} from '../../trace_processor/query_result';
import {
  Flamegraph,
  FLAMEGRAPH_STATE_SCHEMA,
  type FlamegraphState,
} from '../../widgets/flamegraph';
import {z} from 'zod';
import {BOLT_MEMORY_VIEWS_SQL, buildAreaMetricsSql} from './sql';

const STATE_SCHEMA = z.object({
  flamegraphState: FLAMEGRAPH_STATE_SCHEMA.optional(),
});

type PluginState = z.infer<typeof STATE_SCHEMA>;

class BoltMemoryAreaTab implements AreaSelectionTab {
  readonly id = 'dev.bolt.Memory.AreaFlamegraph';
  readonly name = 'Bolt Memory Flamegraph';
  readonly priority = 100;
  private metrics?: ReadonlyArray<QueryFlamegraphMetric>;
  private metricsStart?: bigint;
  private metricsEnd?: bigint;

  constructor(
    private readonly trace: Trace,
    private readonly getState: () => FlamegraphState | undefined,
    private readonly setState: (state: FlamegraphState) => void,
  ) {}

  render(selection: AreaSelection) {
    if (
      this.metrics === undefined ||
      this.metricsStart !== selection.start ||
      this.metricsEnd !== selection.end
    ) {
      this.metrics = createMetrics(selection);
      this.metricsStart = selection.start;
      this.metricsEnd = selection.end;
      this.setState(Flamegraph.updateState(this.getState(), this.metrics));
    }
    const metrics = this.metrics;
    const state =
      this.getState() ?? Flamegraph.createDefaultState(metrics);
    return {
      isLoading: false,
      content: m(FlamegraphPanel, {
        trace: this.trace,
        metrics,
        state,
        onStateChange: this.setState,
      }),
    };
  }
}

function createMetrics(
  selection: AreaSelection,
): ReadonlyArray<QueryFlamegraphMetric> {
  const table = `(${buildAreaMetricsSql(selection)})`;
  return metricsFromTableOrSubquery({
    tableOrSubquery: table,
    tableMetrics: [
      {
        name: 'Live bytes at end',
        unit: 'B',
        columnName: 'live_end_bytes',
      },
      {
        name: 'Allocated bytes in range',
        unit: 'B',
        columnName: 'allocated_bytes',
      },
      {
        name: 'Allocations in range',
        unit: '',
        columnName: 'allocation_count',
      },
      {
        name: 'Byte-seconds in range',
        unit: 'B·s',
        columnName: 'byte_seconds',
      },
    ],
    nameColumnLabel: 'Symbol',
  });
}

export default class BoltMemoryPlugin implements PerfettoPlugin {
  static readonly id = 'dev.bolt.Memory';

  async onTraceLoad(trace: Trace): Promise<void> {
    const result = await trace.engine.query(`
      SELECT COUNT(*) AS count
      FROM slice s
      JOIN track t ON s.track_id = t.id
      WHERE t.name = 'Bolt MemoryPool events'
    `);
    const count = result.firstRow({count: NUM}).count;
    if (count === 0) return;

    await trace.engine.query(BOLT_MEMORY_VIEWS_SQL);
    const store = trace.mountStore<PluginState>(
      BoltMemoryPlugin.id,
      (initial) => STATE_SCHEMA.parse(initial ?? {}),
    );
    trace.selection.registerAreaSelectionTab(
      new BoltMemoryAreaTab(
        trace,
        () => store.state.flamegraphState,
        (state) => {
          store.edit((draft) => {
            draft.flamegraphState = state;
          });
        },
      ),
    );
  }
}
