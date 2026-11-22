<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import DocumentCard from './DocumentCard.vue'
import type { DocumentScope, ProjectFile } from './types'

const props = defineProps<{
  files: ProjectFile[]; activeDocuments: string[]; tableKeys: string[]; locked: boolean; busy: boolean
}>()
const emit = defineEmits<{
  upload: [event: Event]
  'save-binding': [file: ProjectFile, purpose: string, scope: DocumentScope]
  'import-manifest': [manifest: File, documents: File[], done: () => void]
  preview: [file: ProjectFile]
  'delete-file': [file: ProjectFile]
}>()
const activePaths = computed(() => {
  const paths = new Set<string>()
  for (const entry of props.activeDocuments) {
    const path = entry.replaceAll('\\', '/')
    paths.add(path)
    const start = path.lastIndexOf('/files/')
    if (start >= 0) paths.add(path.slice(start + 1))
  }
  return paths
})
const duplicateNames = computed(() => {
  const seen = new Set<string>(), duplicates = new Set<string>()
  for (const file of props.files) {
    if (seen.has(file.name)) duplicates.add(file.name)
    seen.add(file.name)
  }
  return duplicates
})
function isActive(file: ProjectFile) {
  const path = file.relative_path.replaceAll('\\', '/')
  return activePaths.value.has(path) || activePaths.value.has(file.file_id) || activePaths.value.has(file.name)
}

const mdInput = ref<HTMLInputElement | null>(null), scroll = ref<HTMLElement | null>(null)
const importSequence = ref(0)
const importing = ref(false), manifest = ref<File | null>(null), localFiles = ref<File[]>([])
async function toggleImport() {
  importing.value = !importing.value
  if (importing.value) {
    await nextTick()
    if (scroll.value) scroll.value.scrollTop = 0
  }
}
const selected = (event: Event) => [...((event.target as HTMLInputElement).files || [])]
function chooseFiles(event: Event) {
  localFiles.value = selected(event).filter(file => /\.(md|markdown)$/i.test(file.name))
}
function importManifest() {
  if (!manifest.value || props.locked) return
  emit('import-manifest', manifest.value, localFiles.value, () => {
    manifest.value = null
    localFiles.value = []
    importing.value = false
    importSequence.value++
  })
}
const example = `documents:
  - path: docs/orders.md
    type: data_description
    tables:
      - warehouse.public.orders
      - warehouse.public.order_lines
  - path: docs/guide.md
    type: analysis_knowledge`
</script>

<template>
  <section class="document-library" aria-label="知识文档配置">
    <header class="library-tools">
      <div class="library-title"><h2>知识文档</h2><span>{{ files.length }} 份</span></div>
      <div class="actions">
        <button class="btn" :disabled="locked" :aria-expanded="importing" aria-controls="document-import" @click="toggleImport">导入 YAML</button>
        <button class="btn primary" :disabled="locked" @click="mdInput?.click()">上传 Markdown</button>
        <input ref="mdInput" type="file" multiple hidden accept=".md,.markdown" aria-label="上传 Markdown 文档" @change="emit('upload', $event)"/>
      </div>
    </header>
    <div ref="scroll" class="document-scroll" tabindex="0" role="region" aria-label="知识文档列表">
      <form v-show="importing" :key="importSequence" id="document-import" class="manifest" @submit.prevent="importManifest">
        <label class="manifest-field"><span>YAML 清单</span>
          <input type="file" accept=".yaml,.yml" required :disabled="locked" @change="manifest = selected($event)[0] || null"/>
        </label>
        <div class="manifest-files">
          <span>本地 Markdown</span>
          <div class="actions">
            <button type="button" class="btn small" :disabled="locked" @click="($refs.local as HTMLInputElement).click()">选择文件</button>
            <button type="button" class="btn small" :disabled="locked" @click="($refs.directory as HTMLInputElement).click()">选择目录</button>
            <span v-if="localFiles.length">{{ localFiles.length }} 份</span>
          </div>
          <input ref="local" type="file" multiple hidden accept=".md,.markdown" :disabled="locked" @change="chooseFiles"/>
          <input ref="directory" type="file" multiple hidden webkitdirectory :disabled="locked" @change="chooseFiles"/>
          <p class="field-hint">已上传的文档只需清单。新文档可选文件，或选择包含清单所列相对路径的目录。同名文档请填写卡片上的路径。</p>
        </div>
        <details class="manifest-example"><summary>YAML 示例</summary><pre>{{ example }}</pre></details>
        <div class="actions"><button type="submit" class="btn primary small" :disabled="locked || !manifest">应用清单</button></div>
      </form>
      <p v-if="!files.length" class="library-empty">暂无知识文档，可上传 Markdown 或导入 YAML 清单。</p>
      <div v-else class="card-list">
        <DocumentCard v-for="file in files" :key="file.file_id" :file="file" :active="isActive(file)" :show-path="duplicateNames.has(file.name)"
          :table-keys="tableKeys" :locked="locked" :busy="busy"
          @preview="emit('preview', $event)" @remove="emit('delete-file', $event)"
          @save="(doc, purpose, scope) => emit('save-binding', doc, purpose, scope)"/>
      </div>
    </div>
  </section>
</template>

<style scoped>
.document-library { display: flex; flex-direction: column; flex: 1; min-height: 0; min-width: 0; }
.library-tools { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px 14px; padding: 0 0 14px; flex: none; }
.library-title { display: flex; align-items: baseline; gap: 8px; }
.library-title h2 { margin: 0; font-size: 21px; }
.library-title span, .manifest-files > .actions > span { color: #64738a; font-size: 13px; }
.document-scroll { min-height: 0; flex: 1; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; padding: 1px 4px 12px 1px; }
.card-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 380px), 1fr)); align-items: start; gap: 10px; }
.manifest, .library-empty { border: 1px solid var(--line); border-radius: 8px; background: white; padding: 14px 18px; margin: 0 0 12px; }
.manifest, .manifest-field, .manifest-files { display: grid; gap: 6px; min-width: 0; }
.manifest { gap: 14px; }
.manifest .field-hint { margin: 0; }
.manifest-field input { max-width: 100%; }
.manifest-example summary { color: var(--blue); cursor: pointer; width: fit-content; }
.manifest-example pre { overflow: auto; background: #f5f7fb; border-radius: 5px; padding: 10px; font-size: 12px; }
.library-empty { color: #63738c; }
@media(max-width: 600px) {
  .library-tools { padding-bottom: 8px; }
  .library-tools .btn { padding: 6px 8px; }
}
</style>
