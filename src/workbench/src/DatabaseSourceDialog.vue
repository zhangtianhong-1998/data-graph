<script setup lang="ts">
import { computed, ref } from 'vue'
import { updateDatabaseSources } from './database'
import type { DatabaseForm } from './database'
import type { ProjectFile, SourceConfig } from './types'

const props = defineProps<{ source: SourceConfig | null; sources: SourceConfig[]; files: ProjectFile[]; retentionVerified: boolean; busy: boolean; error: string }>()
const emit = defineEmits<{ close: []; save: [sources: SourceConfig[], retentionVerified: boolean] }>()
const form = ref<DatabaseForm>({
  id: props.source?.id || '', kind: props.source?.kind === 'mysql_database' ? 'mysql_database' : 'gauss_database',
  dsnEnv: typeof props.source?.dsn_env === 'string' ? props.source.dsn_env : '',
  tables: Array.isArray(props.source?.allowed_tables) ? props.source.allowed_tables.join('\n') : '',
  capabilityManifest: typeof props.source?.capability_manifest === 'string' ? props.source.capability_manifest : '',
})
const retention = ref(props.retentionVerified)
const localError = ref('')
const manifests = computed(() => props.files.filter(file => file.kind === 'yaml'))
const missingManifest = computed(() => form.value.capabilityManifest && !manifests.value.some(file => file.relative_path === form.value.capabilityManifest))
function submit() {
  localError.value = ''
  try { emit('save', updateDatabaseSources(props.sources, props.source?.id || null, form.value, manifests.value.map(file => file.relative_path)), retention.value) }
  catch (error) { localError.value = error instanceof Error ? error.message : '请检查数据库来源配置。' }
}
</script>

<template>
  <div class="modal-backdrop" @click.self="!busy && emit('close')">
    <section class="modal wide database-modal" role="dialog" aria-modal="true" aria-labelledby="database-title">
      <form @submit.prevent="submit">
        <div class="panel-head"><h2 id="database-title">{{ source ? '编辑数据库来源' : '添加数据库来源' }}</h2><button type="button" class="close" aria-label="关闭" :disabled="busy" @click="emit('close')">×</button></div>
        <div class="panel-body">
          <p v-if="localError || error" class="note amber form-error" role="alert">{{ localError || error }}</p>
          <div class="two-fields">
            <div class="field"><label for="database-kind">数据库类型</label><select id="database-kind" v-model="form.kind" :disabled="busy"><option value="gauss_database">Gauss / openGauss</option><option value="mysql_database">MySQL</option></select></div>
            <div class="field"><label for="database-id">来源编号</label><input id="database-id" v-model="form.id" :disabled="busy || !!source" required maxlength="64" placeholder="例如：business_mysql"/><p class="field-hint">项目内唯一；保存后保持不变，供文档和图谱引用。</p></div>
          </div>
          <div class="field"><label for="database-env">数据库连接环境变量名</label><input id="database-env" v-model="form.dsnEnv" :disabled="busy" required placeholder="例如：LIGHTGRAPH_MYSQL_DSN" autocomplete="off"/><p class="field-hint">由服务器管理员设置连接信息。这里只填写变量名，不输入地址、用户名、密码或完整连接串。</p></div>
          <div class="field"><label for="database-tables">允许查询的表</label><textarea id="database-tables" v-model="form.tables" :disabled="busy" required :placeholder="form.kind === 'mysql_database' ? '每行一张表，包含数据库名，例如：\nanalytics.report_items\nanalytics.organizations' : '每行一张表，包含 schema，例如：\npublic.report_items\npublic.organizations'"/><p class="field-hint">明确列出授权表，不支持通配符。已有列级访问限制会保留，可在完整配置中查看或修改。</p></div>
          <div class="field"><label for="database-manifest">能力验证文件（可选）</label><select id="database-manifest" v-model="form.capabilityManifest" :disabled="busy"><option value="">暂不指定</option><option v-if="missingManifest" :value="form.capabilityManifest" disabled>原文件不在上传列表，请重新选择</option><option v-for="file in manifests" :key="file.file_id" :value="file.relative_path">{{ file.name }}</option></select><p class="field-hint">只能引用此项目已上传的 YAML 文件。此项不等于授权确认，后端仍会核验其中的能力记录。</p></div>
          <div class="retention-box"><label class="checkbox-line"><input v-model="retention" type="checkbox" :disabled="busy"/>此项目的部署环境已完成数据留存检查</label><p class="field-hint">这是项目级确认，影响全部数据库来源。尚未完成时也可保存连接并读取元数据，但不能启动访问真实实例数据的抽取。</p></div>
          <p v-if="source?.allowed_columns" class="field-hint">此来源已有列级访问限制，本次保存会保留原配置。</p>
        </div>
        <div class="panel-footer"><span>保存配置不会主动连接数据库；读取元数据时再连接。</span><button type="submit" class="btn primary" :disabled="busy">保存数据库来源</button></div>
      </form>
    </section>
  </div>
</template>
