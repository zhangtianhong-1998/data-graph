// @vitest-environment happy-dom
import { expect, it } from 'vitest'
import { documentManifestBody } from './documentManifest'

const manifest = new File(['documents: []'], 'documents.yaml')
function localFile(name: string, contents: string, relative = ''): File {
  const file = new File([contents], name)
  Object.defineProperty(file, 'webkitRelativePath', { value: relative })
  return file
}

it('仅发送清单中尚未登记的路径，并区分目录内同名文档', async () => {
  const paths = ['archive/a.md', 'reference/a.md', 'present.md']
  const files = [
    localFile('a.md', 'archive', 'chosen/archive/a.md'),
    localFile('a.md', 'reference', 'chosen/reference/a.md'),
    localFile('private.md', 'unlisted', 'chosen/private.md'),
    localFile('present.md', 'registered', 'chosen/present.md'),
  ]
  const body = documentManifestBody(manifest, { paths, missing_paths: paths.slice(0, 2) }, files)
  const uploads = body.getAll('files') as File[]
  expect(uploads.map(file => file.name)).toEqual(paths.slice(0, 2))
  expect(await Promise.all(uploads.map(file => file.text()))).toEqual(['archive', 'reference'])
})

it('允许唯一的单选文件，遇到同名条目或缺件则在提交前定位路径', () => {
  const file = localFile('guide.md', '# guidance')
  const path = 'docs/guide.md'
  const construct = (paths: string[], files: File[]) => documentManifestBody(manifest, { paths, missing_paths: [path] }, files)
  expect((construct([path], [file]).get('files') as File).name).toBe(path)
  expect(() => construct([path, 'archive/guide.md'], [file])).toThrow(path)
  expect(() => construct([path], [])).toThrow(path)
  expect(() => construct([path], [localFile('guide.md', '', 'chosen/elsewhere/guide.md')])).toThrow(path)
})
