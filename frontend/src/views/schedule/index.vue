<template>
  <section class="page" data-module="schedule">
    <header class="page-head">
      <div>
        <h2>调度编排管理</h2>
        <p class="page-desc">
          计划只能沿「草稿 → 待校验 → 已启用 → 待确认」顺向推进；同窗口争用按业务影响裁决，
          被挤出的计划保留原快照并生成新编号待确认条目，改期结果回写采集任务下次运行时间。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记调度计划</button>
        <button class="btn" type="button" @click="exportRows">导出调度编排清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>计划编号</span>
        <input v-model="filters.keyword" placeholder="按计划编号检索" />
      </label>
      <label class="filter-item">
        <span>阶段</span>
        <select v-model="filters.status">
          <option value="">全部阶段</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <!-- 工作台：待确认计划与改期条目 -->
    <h3 class="section-title">
      工作台 · 待确认计划
      <span class="count-badge">{{ pendingRows.length }}</span>
      <span class="section-actions">
        <button class="btn small" type="button" @click="runRecover">跨日恢复（只重排待确认项）</button>
      </span>
    </h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>条目编号</th>
          <th>来源计划</th>
          <th>关联任务</th>
          <th>原窗口</th>
          <th>改期时间</th>
          <th>原通知渠道</th>
          <th>确认状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in pendingRows" :key="String(row.id)">
          <td>{{ row['计划编号'] }}</td>
          <td>{{ row['来源计划编号'] ?? '—（原计划快照）' }}</td>
          <td>{{ row['关联任务'] ?? '—' }}</td>
          <td>{{ row['原始执行时间'] ?? '—' }}</td>
          <td>{{ row['改期时间'] ?? row['执行时间'] ?? '—' }}<span v-if="row['跨日']" class="muted-text">（次日）</span></td>
          <td>{{ row['原始快照']?.['通知渠道'] ?? row['通知渠道'] ?? '—' }}</td>
          <td>{{ row['确认状态'] ?? '待处理' }}</td>
          <td class="row-actions">
            <template v-if="row['来源计划编号'] && (row['确认状态'] ?? '待处理') === '待处理'">
              <button class="link" type="button" @click="decide(row, '确认改期')">确认改期</button>
              <button class="link" type="button" @click="decide(row, '拒绝改期')">拒绝改期</button>
            </template>
            <button class="link" type="button" @click="viewSnapshot(row)">查看快照</button>
          </td>
        </tr>
        <tr v-if="!pendingRows.length">
          <td colspan="8" class="empty-state">工作台暂无待确认计划</td>
        </tr>
      </tbody>
    </table>

    <!-- 裁决批次 -->
    <h3 class="section-title">
      冲突裁决批次
      <span class="count-badge">{{ batches.length }}</span>
      <span class="section-actions">
        <label class="muted-text">争用窗口（分钟）
          <input v-model.number="windowMinutes" type="number" min="1" style="width: 72px; margin-left: 6px;" />
        </label>
        <button class="btn small primary" type="button" @click="runArbitrate">提交冲突裁决</button>
      </span>
    </h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>裁决批次号</th>
          <th>争用窗口</th>
          <th>占用计划</th>
          <th>改期新编号</th>
          <th>批次状态</th>
          <th>生效失败策略</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="batch in batches" :key="batch['裁决批次号']">
          <td>{{ batch['裁决批次号'] }}</td>
          <td>{{ batch['窗口起点'] }} 起 / {{ batch['窗口时长分钟'] }} 分钟</td>
          <td>{{ batch['占用计划编号'] }}</td>
          <td>{{ (batch['改期条目'] ?? []).join('、') || '—' }}</td>
          <td>{{ batch['状态'] }}</td>
          <td>{{ batch['生效失败策略'] ?? '启动时固化' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="viewBatch(batch)">六维度排名</button>
            <button
              v-if="batch['状态'] === '已裁决'"
              class="link"
              type="button"
              @click="startBatch(batch)"
            >启动批次</button>
          </td>
        </tr>
        <tr v-if="!batches.length">
          <td colspan="7" class="empty-state">暂无裁决批次，提交裁决后这里展示同窗口争用结果</td>
        </tr>
      </tbody>
    </table>

    <!-- 全部调度计划 -->
    <h3 class="section-title">全部调度计划</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>阶段</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td><span class="status-tag" :class="statusClass(row.status)">{{ row.status }}</span></td>
          <td class="row-actions">
            <button
              v-for="action in availableActions(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="viewSnapshot(row)">快照</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无调度编排数据，可先登记调度计划</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条调度编排记录（含改期生成的新编号条目）</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="okMessage" class="ok-text">{{ okMessage }}</span>
    </footer>

    <!-- 登记弹层 -->
    <div v-if="showCreate" class="modal-mask" @click.self="showCreate = false">
      <div class="modal-card">
        <div class="modal-head">
          <h4 class="modal-title">登记调度计划</h4>
          <button class="btn ghost small" type="button" @click="showCreate = false">关闭</button>
        </div>
        <form class="form-grid" @submit.prevent="submitCreate">
          <label>计划编号<input v-model="form['计划编号']" placeholder="如 SCHE-0010" required /></label>
          <label>关联任务<input v-model="form['关联任务']" placeholder="任务编号，如 TASK-0001" required /></label>
          <label>调度表达式<input v-model="form['调度表达式']" placeholder="如 0 9 * * *" required /></label>
          <label>执行时间<input v-model="form['执行时间']" placeholder="HH:MM，如 09:00" /></label>
          <label>优先级<input v-model.number="form['优先级']" type="number" placeholder="数值越大业务影响越高" /></label>
          <label>超时阈值<input v-model="form['超时阈值']" placeholder="如 30秒" /></label>
          <label>失败策略
            <select v-model="form['失败策略']">
              <option value="终止任务">终止任务</option>
              <option value="重试2次">重试2次</option>
              <option value="忽略失败">忽略失败</option>
            </select>
          </label>
          <label>通知渠道
            <select v-model="form['通知渠道']">
              <option value="邮件">邮件</option>
              <option value="短信">短信</option>
              <option value="钉钉">钉钉</option>
              <option value="企业微信">企业微信</option>
            </select>
          </label>
          <div class="modal-foot" style="grid-column: 1 / -1;">
            <button class="btn ghost" type="button" @click="showCreate = false">取消</button>
            <button class="btn primary" type="submit">提交登记</button>
          </div>
        </form>
      </div>
    </div>

    <!-- 批次六维度排名弹层 -->
    <div v-if="activeBatch" class="modal-mask" @click.self="activeBatch = null">
      <div class="modal-card">
        <div class="modal-head">
          <h4 class="modal-title">裁决批次 {{ activeBatch['裁决批次号'] }} · 六维度比较</h4>
          <button class="btn ghost small" type="button" @click="activeBatch = null">关闭</button>
        </div>
        <p class="muted-text">比较维度：{{ (activeBatch['比较维度'] ?? []).join('、') }}；优先级不同只看优先级，优先级相同才继续比较其余维度。</p>
        <table class="data-table">
          <thead>
            <tr><th>名次</th><th>计划编号</th><th>关联任务</th><th>优先级</th><th>超时阈值</th><th>失败策略</th><th>通知渠道</th><th>排序依据</th></tr>
          </thead>
          <tbody>
            <tr v-for="(rank, idx) in activeBatch['排名'] ?? []" :key="rank['计划编号']">
              <td>{{ idx + 1 }}</td>
              <td>{{ rank['计划编号'] }}</td>
              <td>{{ rank['关联任务'] }}</td>
              <td>{{ rank['优先级'] }}</td>
              <td>{{ rank['超时阈值'] }}</td>
              <td>{{ rank['失败策略'] }}</td>
              <td>{{ rank['通知渠道'] }}</td>
              <td class="muted-text">{{ rank['排序依据'] }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 快照弹层 -->
    <div v-if="activeSnapshot" class="modal-mask" @click.self="activeSnapshot = null">
      <div class="modal-card">
        <div class="modal-head">
          <h4 class="modal-title">{{ activeSnapshot['计划编号'] }} · 原始快照</h4>
          <button class="btn ghost small" type="button" @click="activeSnapshot = null">关闭</button>
        </div>
        <div class="snapshot-box">
          <div v-for="(value, key) in (activeSnapshot['原始快照'] ?? activeSnapshot)" :key="String(key)">
            <strong>{{ key }}：</strong>{{ value ?? '—' }}
          </div>
        </div>
        <p v-if="activeSnapshot['裁决理由']" class="muted-text" style="margin-top: 10px;">
          裁决说明：{{ activeSnapshot['裁决理由'] }}
        </p>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

// 后端载荷字段为中文键且结构随裁决/快照场景变化，这里统一用宽松记录类型承载。
type Row = Record<string, any>
type Batch = Record<string, any>
type Snapshot = Record<string, any>

const ENDPOINT = '/api/schedule'
const columns = ['计划编号', '关联任务', '调度表达式', '优先级', '超时阈值', '失败策略', '通知渠道', '执行时间']
const statuses = ['草稿', '待校验', '已启用', '待确认']
// 四阶段顺向推进：每个阶段只暴露唯一合法的下一阶段动作。
const NEXT_ACTION: Record<string, string> = {
  草稿: '提交校验',
  待校验: '启用配置',
  已启用: '进入待确认',
  待确认: '',
}

const rows = ref<Row[]>([])
const total = ref(0)
const pendingRows = ref<Row[]>([])
const batches = ref<Batch[]>([])
const errorMessage = ref('')
const okMessage = ref('')
const filters = ref<{ keyword: string; status: string }>({ keyword: '', status: '' })
const windowMinutes = ref(10)
const showCreate = ref(false)
const activeBatch = ref<Batch | null>(null)
const activeSnapshot = ref<Snapshot | null>(null)
const emptyForm = (): Record<string, string | number> => ({
  计划编号: '', 关联任务: '', 调度表达式: '', 执行时间: '',
  优先级: 1, 超时阈值: '30秒', 失败策略: '重试2次', 通知渠道: '邮件',
})
const form = ref<Record<string, string | number>>(emptyForm())

const stats = ref([
  { label: '调度计划总数', value: 0 },
  { label: '已启用计划', value: 0 },
  { label: '工作台待确认', value: 0 },
  { label: '裁决批次', value: 0 },
])

function statusClass(status: unknown): string {
  if (status === '草稿') return 'is-draft'
  if (status === '待校验') return 'is-review'
  if (status === '已启用') return 'is-enabled'
  return 'is-pending'
}

function availableActions(row: Row): string[] {
  const action = NEXT_ACTION[String(row.status ?? '')]
  return action ? [action] : []
}

function flashOk(message: string) {
  okMessage.value = message
  errorMessage.value = ''
}

function flashError(message: string) {
  errorMessage.value = message
  okMessage.value = ''
}

function resetFilters() {
  filters.value = { keyword: '', status: '' }
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  form.value = emptyForm()
  showCreate.value = true
}

async function submitCreate() {
  try {
    const response = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: form.value }) })
    const payload = await response.json()
    if (!payload.ok) {
      flashError(payload.message ?? '登记失败')
      return
    }
    showCreate.value = false
    flashOk('调度计划已登记，默认进入草稿阶段')
    await reload()
  } catch (error) {
    flashError(error instanceof Error ? error.message : '登记失败')
  }
}

async function runAction(action: string, row: Row) {
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      flashError(payload.message ?? '动作未生效')
      return
    }
    flashOk(payload.message ?? '阶段已推进')
    await reload()
  } catch (error) {
    flashError(error instanceof Error ? error.message : '调度编排操作失败')
  }
}

async function runArbitrate() {
  try {
    const response = await request(`${ENDPOINT}/arbitrate`, {
      method: 'POST',
      body: JSON.stringify({ window_minutes: Number(windowMinutes.value) || 10 }),
    })
    const payload = await response.json()
    flashOk(payload.reused
      ? payload.message
      : `${payload.message}；负方已生成新编号待确认条目并回写任务下次运行时间`)
    await Promise.all([reload(), loadPending(), loadBatches()])
  } catch (error) {
    flashError(error instanceof Error ? error.message : '裁决失败')
  }
}

async function runRecover() {
  try {
    const response = await request(`${ENDPOINT}/recover`, {
      method: 'POST',
      body: JSON.stringify({ window_minutes: Number(windowMinutes.value) || 10 }),
    })
    const payload = await response.json()
    flashOk(`${payload.message}；已启动批次：${(payload.started_batches_untouched ?? []).join('、') || '无'}`)
    await Promise.all([reload(), loadPending(), loadBatches()])
  } catch (error) {
    flashError(error instanceof Error ? error.message : '跨日恢复失败')
  }
}

async function startBatch(batch: Batch) {
  try {
    const response = await request(`${ENDPOINT}/batches/${batch['裁决批次号']}/start`, { method: 'POST' })
    const payload = await response.json()
    if (!payload.ok) {
      flashError(payload.message ?? '批次启动失败')
      return
    }
    flashOk(payload.message ?? '批次已启动，失败策略仅作用于本批次')
    await loadBatches()
  } catch (error) {
    flashError(error instanceof Error ? error.message : '批次启动失败')
  }
}

async function decide(row: Row, decision: string) {
  try {
    const response = await request(`${ENDPOINT}/${row.id}/reschedule-decision`, {
      method: 'POST',
      body: JSON.stringify({ decision }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      flashError(payload.message ?? '确认失败')
      return
    }
    flashOk(payload.message ?? '已处理')
    await Promise.all([reload(), loadPending()])
  } catch (error) {
    flashError(error instanceof Error ? error.message : '确认失败')
  }
}

function viewBatch(batch: Batch) {
  activeBatch.value = batch
}

function viewSnapshot(row: Row) {
  activeSnapshot.value = row as unknown as Snapshot
}

async function reload() {
  const params = new URLSearchParams()
  if (filters.value.keyword) params.set('keyword', filters.value.keyword)
  if (filters.value.status) params.set('status', filters.value.status)
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    if (!response.ok) throw new Error('调度计划列表读取失败')
    const payload = await response.json()
    rows.value = (payload.items ?? []) as Row[]
    total.value = payload.total ?? rows.value.length
    stats.value[0].value = total.value
    stats.value[1].value = rows.value.filter((r) => r.status === '已启用').length
  } catch (error) {
    flashError(error instanceof Error ? error.message : '调度编排列表读取失败')
  }
}

async function loadPending() {
  try {
    const response = await request(`${ENDPOINT}/pending`)
    const payload = await response.json()
    pendingRows.value = (payload.items ?? []) as Row[]
    stats.value[2].value = pendingRows.value.length
  } catch {
    pendingRows.value = []
  }
}

async function loadBatches() {
  try {
    const response = await request(`${ENDPOINT}/batches`)
    const payload = await response.json()
    batches.value = (payload.items ?? []) as Batch[]
    stats.value[3].value = batches.value.length
  } catch {
    batches.value = []
  }
}

onMounted(() => {
  void reload()
  void loadPending()
  void loadBatches()
})
</script>
