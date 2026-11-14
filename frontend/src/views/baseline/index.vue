<template>
  <section class="page" data-module="baseline">
    <header class="page-head">
      <div>
        <h2>基线监控管理</h2>
        <p class="page-desc">维护基线监控的承诺时间、预警机制与关键路径追踪，调整后同步预警窗口、关键路径版本与最近产出。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记基线配置</button>
        <button class="btn" type="button" @click="exportRows">导出基线监控管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>承诺时间从</span>
        <input v-model="commitmentStart" type="datetime-local" />
      </label>
      <label class="filter-item">
        <span>承诺时间到</span>
        <input v-model="commitmentEnd" type="datetime-local" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>版本</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column" :class="{ 'cell-stale': isStale(row, column) }">
            {{ display(row, column) }}
          </td>
          <td>{{ row['关键路径版本'] ?? 'v1' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :disabled="!canRun(action, row)"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="openDetail(row)">查看详情</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无基线监控管理数据，可先登记基线配置</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条基线监控管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-if="statusMessage" class="ok-text">{{ statusMessage }}</span>
    </footer>

    <form v-if="creating" class="drawer" @submit.prevent="submitCreate">
      <h3>登记基线配置</h3>
      <label class="filter-item">
        <span>基线编号</span>
        <input v-model="draft['基线编号']" placeholder="BASL-0004" required />
      </label>
      <label class="filter-item">
        <span>基线名称</span>
        <input v-model="draft['基线名称']" placeholder="基线名称" required />
      </label>
      <label class="filter-item">
        <span>承诺时间</span>
        <input v-model="draft['承诺时间']" placeholder="2026-09-04 18:00" required />
      </label>
      <label class="filter-item">
        <span>预警提前分钟</span>
        <input v-model="draft['预警窗口']" placeholder="30" />
      </label>
      <label class="filter-item">
        <span>关键路径</span>
        <input v-model="draft['关键路径']" placeholder="DAG-0004" />
      </label>
      <label class="filter-item">
        <span>依赖范围</span>
        <input v-model="draft['依赖范围']" placeholder="SLA-0004" />
      </label>
      <div class="row-actions">
        <button class="btn primary" type="submit">提交</button>
        <button class="btn ghost" type="button" @click="creating = false">取消</button>
      </div>
    </form>

    <aside v-if="detail" class="drawer" data-role="baseline-detail">
      <h3>{{ detail['基线编号'] }} · {{ detail['基线名称'] }}</h3>
      <p class="page-desc">监控状态：{{ detail['监控状态'] ?? detail.status }}</p>
      <dl class="detail-grid">
        <template v-for="field in detailFields" :key="field">
          <dt>{{ field }}</dt>
          <dd :class="{ 'cell-stale': isStale(detail, field) }">{{ display(detail, field) }}</dd>
        </template>
      </dl>

      <form v-if="detail.status === '监控中'" @submit.prevent="submitAdjust">
        <h4>调整承诺时间</h4>
        <label class="filter-item">
          <span>新承诺时间</span>
          <input v-model="adjustDraft['承诺时间']" placeholder="2026-09-03 20:00" required />
        </label>
        <label class="filter-item">
          <span>预警提前分钟（留空沿用原窗口）</span>
          <input v-model="adjustDraft['预警窗口']" placeholder="30" />
        </label>
        <label class="filter-item">
          <span>最近产出（留空跟进新承诺时间）</span>
          <input v-model="adjustDraft['最近产出']" placeholder="2026-09-03 20:00" />
        </label>
        <div class="row-actions">
          <button class="btn primary" type="submit">提交调整</button>
          <button class="btn ghost" type="button" @click="reload">刷新</button>
        </div>
      </form>

      <h4>版本历史</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>版本</th>
            <th>承诺时间</th>
            <th>预警时间</th>
            <th>关键路径版本</th>
            <th>最近产出</th>
            <th>是否有效</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="version in versions" :key="String(version['版本ID'])">
            <td>{{ version['版本号'] }}</td>
            <td>{{ version['承诺时间'] }}</td>
            <td>{{ version['预警时间'] }}</td>
            <td>{{ version['关键路径版本'] }}</td>
            <td>{{ version['最近产出'] }}</td>
            <td>{{ version['是否有效'] ? '有效' : '历史' }}</td>
          </tr>
          <tr v-if="!versions.length">
            <td colspan="6" class="empty-state">暂无版本记录</td>
          </tr>
        </tbody>
      </table>

      <div class="row-actions">
        <button class="btn ghost" type="button" @click="detail = null">关闭详情</button>
      </div>
    </aside>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null | undefined>
type Version = Record<string, string | number | boolean | null>

const ENDPOINT = '/api/baseline'
const columns = ["基线编号", "基线名称", "承诺时间", "预警时间", "预警窗口", "风险窗口", "关键路径", "关键路径版本", "依赖范围", "监控状态", "最近产出", "核对结果"]
const detailFields = ["基线编号", "基线名称", "承诺时间", "预警时间", "预警窗口", "风险窗口", "关键路径", "关键路径版本", "依赖范围", "监控状态", "最近产出", "核对结果"]
const actions = ["启用监控", "调整基线", "关闭基线"]
const stats = ref([{"label": "基线配置总数", "value": 0}, {"label": "监控中基线", "value": 0}, {"label": "已延期基线", "value": 0}])

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const statusMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 2)
const commitmentStart = ref('')
const commitmentEnd = ref('')
const detail = ref<Row | null>(null)
const versions = ref<Version[]>([])
const creating = ref(false)
const draft = ref<Record<string, string>>({})
const adjustDraft = ref<Record<string, string>>({})

function resetFilters() {
  filters.value = {}
  commitmentStart.value = ''
  commitmentEnd.value = ''
  void reload()
}

/** 导出：范围与当前承诺时间筛选一致，只导当前有效版本。 */
function exportRows() {
  const query = new URLSearchParams()
  if (commitmentStart.value) query.set('commitment_start', commitmentStart.value.replace('T', ' '))
  if (commitmentEnd.value) query.set('commitment_end', commitmentEnd.value.replace('T', ' '))
  query.set('format', 'csv')
  query.set('download', 'true')
  window.open(`${ENDPOINT}/export?${query.toString()}`, '_blank')
}

function canRun(action: string, row: Row) {
  const status = String(row.status ?? row['监控状态'] ?? '')
  if (status === '已关闭') return false
  if (action === '启用监控') return status === '草稿'
  if (action === '调整基线') return status === '监控中'
  if (action === '关闭基线') return status === '已延期'
  return true
}

function display(row: Row | null, column: string) {
  if (!row) return '—'
  if (column === '监控状态') return row['监控状态'] ?? row.status ?? '—'
  const value = row[column]
  return value === undefined || value === null || value === '' ? '—' : String(value)
}

/** 预警时间与承诺时间不一致时标红，提示旧预警残留。 */
function isStale(row: Row | null, column: string) {
  if (!row || column !== '预警时间') return false
  const commitment = String(row['承诺时间'] ?? '')
  const warning = String(row['预警时间'] ?? '')
  if (!commitment || !warning) return false
  const lead = Number.parseInt(String(row['预警窗口'] ?? ''), 10)
  if (!Number.isFinite(lead)) return false
  const toMinutes = (value: string) => {
    const match = value.match(/(\d{1,2}):(\d{2})/)
    return match ? Number(match[1]) * 60 + Number(match[2]) : null
  }
  const cm = toMinutes(commitment)
  const wm = toMinutes(warning)
  if (cm === null || wm === null) return false
  const delta = (cm - wm + 1440) % 1440
  return delta !== lead
}

function openCreate() {
  creating.value = true
  draft.value = {}
}

async function submitCreate() {
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: draft.value }),
    })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.message ?? '基线配置登记失败')
    creating.value = false
    statusMessage.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '基线配置登记失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  statusMessage.value = ''
  if (action === '调整基线') {
    openDetail(row)
    return
  }
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.message ?? '基线监控管理动作未生效')
    statusMessage.value = payload.message
    await reload()
    if (detail.value) await openDetail(detail.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '基线监控管理操作失败'
  }
}

async function submitAdjust() {
  if (!detail.value) return
  errorMessage.value = ''
  statusMessage.value = ''
  const values: Record<string, string> = {
    action: '调整基线',
    operator: '值班管理员',
    request_id: `${detail.value.id}-${adjustDraft.value['承诺时间']}-${adjustDraft.value['预警窗口'] ?? ''}`,
  }
  if (adjustDraft.value['承诺时间']) values['承诺时间'] = adjustDraft.value['承诺时间']
  if (adjustDraft.value['预警窗口']) values['预警窗口'] = adjustDraft.value['预警窗口']
  if (adjustDraft.value['最近产出']) values['最近产出'] = adjustDraft.value['最近产出']
  try {
    const response = await request(`${ENDPOINT}/${detail.value.id}/actions`, {
      method: 'POST',
      body: JSON.stringify(values),
    })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.message ?? '基线调整未生效')
    statusMessage.value = payload.message
    adjustDraft.value = {}
    await reload()
    await openDetail(payload.entry)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '基线调整失败'
  }
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  if (!row.id) return
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) throw new Error('基线详情读取失败')
    detail.value = await response.json()
    const versionResponse = await request(`${ENDPOINT}/${row.id}/versions`)
    const payload = await versionResponse.json()
    versions.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '基线详情读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (filters.value['基线编号']) query.set('keyword', filters.value['基线编号'])
  if (commitmentStart.value) query.set('commitment_start', commitmentStart.value.replace('T', ' '))
  if (commitmentEnd.value) query.set('commitment_end', commitmentEnd.value.replace('T', ' '))
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) {
      throw new Error('基线配置列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value = [
      { label: '基线配置总数', value: total.value },
      { label: '监控中基线', value: rows.value.filter((row) => (row['监控状态'] ?? row.status) === '监控中').length },
      { label: '已延期基线', value: rows.value.filter((row) => (row['监控状态'] ?? row.status) === '已延期').length },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '基线监控管理列表读取失败'
  }
}

onMounted(reload)
</script>
