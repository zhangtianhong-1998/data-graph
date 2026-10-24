import { isDatabaseSource } from './database'
import type { SourceConfig } from './types'

export const workbenchLocationKey = 'data-graph.workbench-location'
type Location = { projectId: string; databaseSourceId?: string | null }
type LocationStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>
const identifier = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 256 && !/[\x00-\x1f\x7f]/.test(value)

/** Only navigation identifiers belong in the browser; connection drafts stay encrypted on the server. */
export class WorkbenchLocation {
  constructor(private storage: () => LocationStorage = () => window.sessionStorage) {}

  private read(): Location | null {
    try {
      const raw = this.storage().getItem(workbenchLocationKey)
      if (raw === null) return null
      const value: unknown = JSON.parse(raw)
      if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid_location')
      const location = value as Record<string, unknown>
      if (!identifier(location.projectId) || Object.keys(location).some(key => !['projectId', 'databaseSourceId'].includes(key))) throw new Error('invalid_location')
      if ('databaseSourceId' in location && location.databaseSourceId !== null && !identifier(location.databaseSourceId)) throw new Error('invalid_location')
      return location as Location
    } catch {
      this.clear()
      return null
    }
  }

  private write(value: Location) {
    // Construct the payload explicitly: never serialize source objects or connection forms.
    const location: Location = { projectId: value.projectId }
    if ('databaseSourceId' in value) location.databaseSourceId = value.databaseSourceId
    try { this.storage().setItem(workbenchLocationKey, JSON.stringify(location)) } catch { /* Storage can be disabled. */ }
  }

  private clear() {
    try { this.storage().removeItem(workbenchLocationKey) } catch { /* Storage can be disabled. */ }
  }

  preferredProject(projects: ReadonlyArray<{ project_id: string }>): string | undefined {
    const location = this.read()
    if (location && projects.some(project => project.project_id === location.projectId)) return location.projectId
    if (location) this.clear()
    return projects[0]?.project_id
  }

  removeProject(projectId: string) {
    if (this.read()?.projectId === projectId) this.clear()
  }

  selectProject(projectId: string) {
    if (this.read()?.projectId !== projectId) this.write({ projectId })
  }

  openDatabase(projectId: string, sourceId: string | null) {
    this.write({ projectId, databaseSourceId: sourceId })
  }

  closeDatabase(projectId: string) {
    if (this.read()?.projectId === projectId) this.write({ projectId })
  }

  restoreDatabase(projectId: string, sources: readonly SourceConfig[]): { source: SourceConfig | null } | null {
    const location = this.read()
    if (!location || location.projectId !== projectId || !('databaseSourceId' in location)) return null
    if (location.databaseSourceId === null) return { source: null }
    const source = sources.find(source => source.id === location.databaseSourceId && isDatabaseSource(source))
    if (source) return { source }
    this.closeDatabase(projectId)
    return null
  }
}
