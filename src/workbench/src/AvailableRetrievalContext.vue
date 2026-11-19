<script setup lang="ts">
import { computed } from 'vue'
import type { AvailableRetrievalContext } from './types'

const props = defineProps<{ context: AvailableRetrievalContext }>()
const labels = computed(() => new Map(props.context.nodes.map(n => [n.id, n.display_name || n.id])))
const pathLabels = computed(() => new Map(props.context.candidate_path?.nodes.map(n => [n.id, n.display_name || n.id])))
const statusText = {
  source_text_only: '当前只找到相关原文，尚未确认对应实体。',
  no_confirmed_match: '当前可见范围内未能确认匹配的实体或事实。',
  scope_unresolved: '尚未确认问题所指的适用范围。',
  search_unavailable: '语义检索当前不可用，未能核实本次问题。',
}
</script>

<template>
  <section class="available-context" aria-label="可用依据">
    <template v-if="context.status === 'partial'">
      <h3>只符合部分条件</h3>
      <ul class="context-nodes">
        <li v-for="node in context.nodes" :key="node.id">{{ node.display_name || node.id }}</li>
      </ul>
      <details v-if="context.edges.length || context.witnesses.length" class="context-proofs">
        <summary>查看关系与来源记录</summary>
        <ul v-if="context.edges.length">
          <li v-for="edge in context.edges" :key="edge.id">
            {{ labels.get(edge.source) || edge.source }} → {{ edge.relation }} → {{ labels.get(edge.target) || edge.target }}
          </li>
        </ul>
        <p v-for="(witness, index) in context.witnesses" :key="index">
          {{ witness.conditions.map(c => c.text).join('、') }}
          <small><template v-if="witness.source">{{ [witness.source.source_id, witness.source.schema, witness.source.table].filter(Boolean).join('.') }} · </template>来源行 {{ witness.row_id }}</small>
        </p>
      </details>
      <p v-if="context.truncated" class="field-hint">已达到本次查询上限，部分依据未展示。</p>
    </template>
    <p v-else role="status">{{ statusText[context.status] }}</p>
    <section v-if="context.candidate_path" aria-label="候选路径" class="candidate-path">
      <h3>候选路径</h3>
      <p>待确认的假设 · {{ context.candidate_path.assumptions.map(a => '“' + a.text + '”指“' + a.display_name + '”').join('、') }}</p>
      <p v-if="context.candidate_path.level_node_ids.length">假设成立时，对应的{{ context.candidate_path.requested_level }}为
        {{ context.candidate_path.level_node_ids.map(id => pathLabels.get(id) || id).join('、') }}</p>
      <details>
        <summary>查看支撑这条候选路径的记录</summary>
        <ul><li v-for="edge in context.candidate_path.edges" :key="edge.id">
          {{ pathLabels.get(edge.source) || edge.source }} → {{ edge.relation }} → {{ pathLabels.get(edge.target) || edge.target }}
        </li></ul>
        <p v-for="(witness, index) in context.candidate_path.witnesses" :key="index">
          <template v-if="witness.source">{{ [witness.source.source_id, witness.source.schema, witness.source.table].filter(Boolean).join('.') }} · </template>来源行 {{ witness.row_id }}
        </p>
      </details>
    </section>
    <section v-if="context.passages.length" aria-label="来源原文">
      <h3>来源原文</h3>
      <figure v-for="(passage, index) in context.passages" :key="index">
        <figcaption>{{ passage.display_name || passage.node_id }} · 提及“{{ passage.text }}”</figcaption>
        <blockquote>{{ passage.excerpt }}</blockquote>
        <small v-if="passage.source_tables.length">来源表 {{ passage.source_tables.join('、') }}</small>
        <small v-if="passage.evidence_refs.length">引用 {{ passage.evidence_refs.join('、') }}</small>
        <small v-else-if="passage.observation_id">来源记录 {{ passage.observation_id }}</small>
      </figure>
    </section>
  </section>
</template>

<style scoped>
.available-context { margin-block: 12px; overflow-wrap: anywhere }
.context-nodes { display: flex; flex-wrap: wrap; gap: 4px 16px; list-style: none; padding: 0 }
.candidate-path { margin-block: 12px; padding-inline-start: 12px; border-inline-start: 2px solid var(--line) }
.available-context small { display: block; color: var(--muted); margin-block: 2px }
.available-context figure { margin: 8px 0 14px }
.available-context blockquote { margin: 4px 0; padding: 3px 10px; border-left: 2px solid var(--line); white-space: pre-wrap }
</style>
