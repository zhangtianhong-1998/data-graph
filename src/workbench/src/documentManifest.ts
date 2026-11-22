type ManifestSelection = { paths: string[]; missing_paths: string[] }

/** A selected folder is the manifest root. Send only explicitly referenced Markdown files. */
export function documentManifestBody(manifest: File, selection: ManifestSelection, files: File[]): FormData {
  const body = new FormData()
  body.append('manifest', manifest)
  const paths = new Map<string, File[]>()
  const looseFiles = new Map<string, File[]>()
  for (const file of files) {
    const relative = file.webkitRelativePath
    const key = relative ? relative.slice(relative.indexOf('/') + 1) : file.name
    const lookup = relative ? paths : looseFiles
    const group = lookup.get(key)
    if (group) group.push(file)
    else lookup.set(key, [file])
  }
  const names = new Map<string, number>()
  for (const path of selection.paths) {
    const name = path.split('/').pop()!
    names.set(name, (names.get(name) || 0) + 1)
  }
  for (const path of selection.missing_paths) {
    const name = path.split('/').pop()!
    const matches = paths.get(path) || (names.get(name) === 1 ? looseFiles.get(name) : undefined)
    if (matches?.length !== 1) {
      throw new Error(`请为清单中的“${path}”选择对应文档。若有同名文件，请选择包含这些相对路径的根目录。`)
    }
    body.append('files', matches[0]!, path)
  }
  return body
}
