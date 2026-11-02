<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ values: Record<string, unknown>; label?: (key: string) => string }>()
const entries = computed(() => Object.entries(props.values).filter(([, value]) => value !== undefined))
const scalar = (value: unknown) => value === null ? '未提供' : value === true ? '是' : value === false ? '否' : String(value)
</script>

<template>
  <table v-if="entries.length" class="property-table"><tbody>
    <tr v-for="[key, value] in entries" :key="key">
      <th scope="row">{{ label ? label(key) : key }}</th>
      <td>
        <template v-if="Array.isArray(value)">
          <span v-if="!value.length" class="muted">无</span>
          <ul v-else class="property-values"><li v-for="(item, i) in value" :key="i">
            <PropertyTable v-if="item !== null && typeof item === 'object'" :values="item" :label="label" />
            <template v-else>{{ scalar(item) }}</template>
          </li></ul>
        </template>
        <PropertyTable v-else-if="value !== null && typeof value === 'object'" :values="value as Record<string, unknown>" :label="label" />
        <template v-else>{{ scalar(value) }}</template>
      </td>
    </tr>
  </tbody></table>
  <p v-else class="muted small-text">暂无属性</p>
</template>

<style scoped>
.property-table{width:100%;border-collapse:collapse;font-size:12px;table-layout:fixed}.property-table th,.property-table td{padding:7px 5px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left;overflow-wrap:anywhere;white-space:normal}.property-table th{width:32%;font-weight:500;color:var(--muted)}.property-values{margin:0;padding:0 0 0 16px}.property-values li+li{margin-top:4px}.property-table .property-table th{width:38%}
</style>
