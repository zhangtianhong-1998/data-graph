<script setup lang="ts">
import { ref } from 'vue'
import type { EntityFusionRule, FusionPolicy } from './types'

const props = defineProps<{ modelValue: FusionPolicy; locked: boolean; title?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: FusionPolicy] }>()
const draft = ref<EntityFusionRule | null>(null), index = ref(-1), keys = ref(''), aliases = ref(''), error = ref('')
function edit(i = -1) {
  index.value = i; error.value = ''
  const rule: EntityFusionRule = JSON.parse(JSON.stringify(props.modelValue.entity_rules[i] ||
    { entity_type: '', canonical_type: '', identity_domain: '', key_mapping: {}, property_aliases: {} }))
  draft.value = rule
  const format = (pairs: Record<string,string>) => Object.entries(pairs).map(([a,b]) => a + ' = ' + b).join('\n')
  keys.value = format(rule.key_mapping); aliases.value = format(rule.property_aliases || {})
}
function pairs(text: string) {
  const result: Record<string,string> = Object.create(null)
  for (const line of text.split('\n').map(s => s.trim()).filter(Boolean)) {
    const parts = line.split('=').map(s => s.trim())
    if (parts.length !== 2 || !parts[0] || !parts[1] || Object.hasOwn(result, parts[0])) throw new Error('每行填写一组映射，等号两侧不能为空，左侧名称不能重复。')
    result[parts[0]] = parts[1]
  }
  return result
}
function ruleKey(rule: EntityFusionRule) { return JSON.stringify([rule.entity_type, Object.values(rule.key_mapping).sort()]) }
function apply() {
  if (!draft.value) return
  try {
    const rule = { ...draft.value, key_mapping: pairs(keys.value), property_aliases: pairs(aliases.value) }
    if (!rule.entity_type.trim() || !rule.canonical_type.trim() || !rule.identity_domain.trim() || !Object.keys(rule.key_mapping).length) throw new Error('填写实体类型、统一类型、身份域和至少一个身份键。')
    const rules = [...props.modelValue.entity_rules]
    if (rules.some((r,i) => i !== index.value && ruleKey(r) === ruleKey(rule))) throw new Error('相同实体类型和原身份键已有规则，请编辑已有规则。')
    if (rules.some((r,i) => i !== index.value && r.entity_type === rule.entity_type && (r.canonical_type !== rule.canonical_type || r.identity_domain !== rule.identity_domain))) throw new Error('同一原实体类型的规则须使用相同的统一类型和身份域。')
    if (index.value < 0) rules.push(rule); else rules[index.value] = rule
    emit('update:modelValue', { ...props.modelValue, entity_rules: rules }); draft.value = null
  } catch (e) { error.value = (e as Error).message }
}
function remove(i: number) {
  draft.value = null
  emit('update:modelValue', { ...props.modelValue, entity_rules: props.modelValue.entity_rules.filter((_,j) => j !== i) })
}
</script>
<template>
  <details class="panel fusion-editor"><summary>{{ title || '项目跨源实体合并规则' }}（{{ modelValue.entity_rules.length }}）</summary>
    <fieldset :disabled="locked">
      <label>同一身份出现不同名称时
        <select :value="modelValue.label_conflict_policy" @change="emit('update:modelValue', { ...modelValue, label_conflict_policy: ($event.target as HTMLSelectElement).value as FusionPolicy['label_conflict_policy'] })">
          <option value="retain_variants">保留实体、各名称及其来源</option><option value="quarantine">隔离冲突实体</option><option value="strict">阻止发布</option>
        </select>
      </label>
      <p class="field-hint">只有使用同一编码体系的实体才应共享身份。不同来源可分别声明映射，统一类型、身份域和规范键相同后再合并。</p>
      <div v-for="(rule,i) in modelValue.entity_rules" :key="ruleKey(rule)" class="actions">
        <span>{{ rule.entity_type }}（{{ Object.values(rule.key_mapping).join('、') }}） → {{ rule.canonical_type }}（{{ rule.identity_domain }}）</span>
        <button class="btn small" @click="edit(i)">编辑</button><button class="text-button" @click="remove(i)">移除</button>
      </div>
      <button v-if="!draft" class="btn small" @click="edit()">添加实体规则</button>
      <template v-if="draft">
        <label>原实体类型<input v-model.trim="draft.entity_type" placeholder="如 Item"/></label>
        <label>统一实体类型<input v-model.trim="draft.canonical_type" placeholder="如 Product"/></label>
        <label>共享身份域<input v-model.trim="draft.identity_domain" placeholder="如 产品主数据"/></label>
        <label>身份键：规范键 = 原节点身份键（每行一组）<textarea v-model="keys" placeholder="product_id = item_code"/></label>
        <label>属性别名：原属性 = 规范属性（可选）<textarea v-model="aliases" placeholder="产品品类 = 类别"/></label>
        <p class="field-hint">不同表里的同名属性含义不同时，左侧可写“来源编号.模式.表名.原属性”，例如给实际收入与预算收入各自命名。</p>
        <p v-if="error" role="alert">{{ error }}</p>
        <button class="btn small" @click="apply">应用到工作流草稿</button><button class="text-button" @click="draft = null">取消编辑</button>
      </template>
    </fieldset>
  </details>
</template>
<style scoped>
.fusion-editor { padding: 12px 16px; margin-bottom: 14px } summary { cursor: pointer } fieldset { border: 0; margin: 12px 0 0; padding: 0 }
label { display: grid; gap: 4px; margin: 8px 0; max-width: 560px }
</style>
