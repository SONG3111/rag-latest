<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  CalendarOutlined,
  CheckOutlined,
  CloseOutlined,
  LinkOutlined,
  LockOutlined,
  MessageOutlined,
  PlusOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
  WarningOutlined,
} from '@ant-design/icons-vue'
import { useNewsStore } from '@/stores/news'
import type { NewsEvent } from '@/types'
import NewsChatPanel from './NewsChatPanel.vue'

const store = useNewsStore()
const customDraft = ref('')
const chatOpen = ref(false)
const manualGeneration = ref(false)
const drawerCloseButton = ref<HTMLButtonElement | null>(null)

const digest = computed(() => store.digest)
const processing = computed(
  () => manualGeneration.value || store.refreshing || store.hasProcessingDigest,
)
const groups = computed(() => {
  const result: { id: string; label: string; events: NewsEvent[] }[] = []
  const byCategory = new Map<string, NonNullable<typeof digest.value>['events']>()
  for (const event of digest.value?.events || []) {
    const items = byCategory.get(event.category) || []
    items.push(event)
    byCategory.set(event.category, items)
  }
  const order = [
    ...store.selectedCategories,
    ...store.customTopics.map((topic) => `custom:${topic}`),
  ]
  for (const category of order) {
    const events = byCategory.get(category)
    if (!events?.length) continue
    result.push({ id: category, label: events[0].category_label, events })
    byCategory.delete(category)
  }
  return result
})
const visibleEventCount = computed(() =>
  groups.value.reduce((total, group) => total + group.events.length, 0),
)

onMounted(() => {
  void store.bootstrap()
})

async function onDrawerOpenChange(open: boolean) {
  if (open) {
    await nextTick()
    window.requestAnimationFrame(() => drawerCloseButton.value?.focus())
  } else {
    document.querySelector<HTMLElement>('.mobile-chat-trigger')?.focus()
  }
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return '尚未生成'
  return new Date(value).toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function formatDate(value: string): string {
  if (!value) return ''
  return new Date(`${value}T00:00:00+08:00`).toLocaleDateString('zh-CN', {
    month: 'long',
    day: 'numeric',
    weekday: 'short',
  })
}

function statusText(): string {
  if (store.refreshing) return '正在采集与整理'
  if (digest.value?.status === 'pending' || digest.value?.status === 'running') {
    return '后台生成中'
  }
  if (digest.value?.status === 'failed') return '生成失败'
  if (digest.value?.status === 'ready') return '快照已就绪'
  return '还没有快照'
}

function statusColor(): string {
  if (digest.value?.status === 'failed') return 'error'
  if (processing.value) return 'processing'
  if (digest.value?.status === 'ready') return 'success'
  return 'default'
}

async function toggleCategory(id: string) {
  try {
    await store.toggleCategory(id)
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}

async function addTopic() {
  try {
    await store.addCustomTopic(customDraft.value)
    customDraft.value = ''
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}

async function removeTopic(topic: string) {
  try {
    await store.removeCustomTopic(topic)
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}

async function refresh() {
  if (processing.value) return
  if (!store.hasSelection) {
    message.warning('请先选择至少一个行业或主题')
    return
  }
  manualGeneration.value = true
  try {
    await store.refreshDigest()
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    manualGeneration.value = false
  }
}

async function changeDate(event: Event) {
  const value = (event.target as HTMLInputElement).value
  if (value) await store.loadDigest(value)
}
</script>

<template>
  <div class="news-shell">
    <aside class="panel news-filters">
      <div class="panel-header">
        <span>新闻雷达</span>
        <span aria-live="polite">
          <a-tag :color="statusColor()" size="small">{{ statusText() }}</a-tag>
        </span>
      </div>
      <div
        class="panel-body filters-body"
        :class="{ 'is-processing': processing }"
        :aria-busy="processing ? 'true' : 'false'"
      >
        <div v-if="processing" class="filter-lock" role="status">
          <LockOutlined aria-hidden="true" />
          <span>生成中，筛选条件已锁定</span>
        </div>
        <section class="filter-section">
          <div class="section-heading">
            <span>关注行业</span>
            <span class="section-note">
              已选 {{ store.selectedCategories.length }}/{{ store.categories.length }}
            </span>
          </div>
          <div class="category-actions">
            <button
              type="button"
              :disabled="store.savingPreferences || processing"
              @click="store.selectAllCategories()"
            >
              全选
            </button>
            <button
              type="button"
              :disabled="store.savingPreferences || processing"
              @click="store.resetDefaultCategories()"
            >
              恢复默认
            </button>
          </div>
          <div v-if="store.error" class="filter-error" role="alert">
            {{ store.error }}
          </div>
          <div class="category-grid" role="group" aria-label="选择关注行业">
            <button
              v-for="category in store.categories"
              :key="category.id"
              type="button"
              class="category-option"
              :class="{ selected: store.selectedCategories.includes(category.id) }"
              :aria-pressed="store.selectedCategories.includes(category.id)"
              :title="category.description"
              :disabled="store.savingPreferences || processing"
              @click="toggleCategory(category.id)"
            >
              <span class="category-name">{{ category.label }}</span>
              <span class="category-check" aria-hidden="true">
                <CheckOutlined v-if="store.selectedCategories.includes(category.id)" />
              </span>
            </button>
          </div>
        </section>

        <section class="filter-section custom-section">
          <div class="section-heading">
            <span>自定义主题</span>
            <span class="section-note">{{ store.customTopics.length }}/{{ store.customTopicLimit }}</span>
          </div>
          <div class="custom-input-row">
            <a-input
              v-model:value="customDraft"
              size="small"
              name="news-custom-topic"
              autocomplete="off"
              aria-label="添加自定义新闻主题"
              placeholder="例如：机器人、低空经济…"
              :disabled="processing || store.customTopics.length >= store.customTopicLimit"
              @press-enter="addTopic"
            />
            <a-button
              size="small"
              aria-label="添加自定义新闻主题"
              :disabled="processing || !customDraft.trim() || store.customTopics.length >= store.customTopicLimit"
              @click="addTopic"
            >
              <template #icon><PlusOutlined /></template>
            </a-button>
          </div>
          <div v-if="store.customTopics.length" class="topic-chips">
            <a-tag
              v-for="topic in store.customTopics"
              :key="topic"
              :closable="!processing"
              color="blue"
              @close="removeTopic(topic)"
            >
              {{ topic }}
            </a-tag>
          </div>
        </section>

        <section class="filter-section date-section">
          <div class="section-heading"><span>快照日期</span></div>
          <label class="date-control" :class="{ disabled: processing }">
            <CalendarOutlined />
            <input
              type="date"
              name="news-snapshot-date"
              aria-label="新闻快照日期"
              :value="store.selectedDate"
              :disabled="processing"
              @change="changeDate"
            />
          </label>
          <a-button
            type="primary"
            block
            :loading="processing"
            :disabled="processing || !store.hasSelection"
            @click="refresh"
          >
            <template #icon><ReloadOutlined /></template>
            {{ digest?.status === 'ready' ? '刷新当前热点' : '生成当前热点' }}
          </a-button>
        </section>

        <section class="schedule-note">
          <ThunderboltOutlined />
          <span>
            提问时按当前选择自动生成新闻；{{ store.health?.cache_minutes || 15 }}
            分钟内追问复用缓存，新闻历史保留 30 天。
          </span>
        </section>
      </div>
    </aside>

    <main class="panel news-feed">
      <div class="panel-header feed-header">
        <div>
          <h1 class="feed-title">{{ formatDate(store.selectedDate) }} · 新闻事件</h1>
          <div class="feed-subtitle">
            {{ groups.length }} 个分类 · {{ visibleEventCount }} 个事件
            <template v-if="digest?.completed_at">
              · 更新于 {{ formatTimestamp(digest.completed_at) }}
            </template>
          </div>
        </div>
        <a-button class="mobile-chat-trigger" size="small" @click="chatOpen = true">
          <template #icon><MessageOutlined /></template>
          追问
        </a-button>
      </div>

      <div class="panel-body feed-body" :aria-busy="processing ? 'true' : 'false'">
        <div v-if="store.loading" class="feed-state">
          <a-spin size="large" />
          <span>正在载入新闻雷达…</span>
        </div>

        <div v-else-if="processing" class="feed-state processing-state">
          <span class="pulse-mark"><ReloadOutlined spin /></span>
          <strong>正在汇总多来源报道</strong>
          <span>完成抓取、去重、事件聚类和中文摘要后会自动显示。</span>
        </div>

        <div v-else-if="!digest" class="feed-state empty-state">
          <span class="state-icon"><ThunderboltOutlined /></span>
          <strong>还没有 {{ formatDate(store.selectedDate) }} 的新闻快照</strong>
          <span>直接向新闻助手提问即可按当前行业生成新闻，也可以手动刷新。</span>
          <a-button type="primary" :disabled="!store.hasSelection" @click="refresh">
            生成当前热点
          </a-button>
        </div>

        <template v-else>
          <div
            v-if="digest.warnings?.length || digest.degraded"
            class="warning-strip"
            role="alert"
          >
            <WarningOutlined />
            <div>
              <strong>{{ digest.degraded ? '部分内容使用了降级摘要' : '部分来源暂不可用' }}</strong>
              <span v-if="digest.warnings?.length">{{ digest.warnings[0] }}</span>
            </div>
          </div>

          <div v-if="digest.events.length === 0" class="feed-state empty-state compact">
            <span class="state-icon"><CalendarOutlined /></span>
            <strong>这个时间窗口没有达到重要性门槛的新事件</strong>
            <span>系统不会用低质量内容填充版面。可以稍后刷新或查看其他日期。</span>
          </div>

          <section v-for="group in groups" :key="group.id" class="event-group">
            <div class="group-heading">
              <h2>{{ group.label }}</h2>
              <span>{{ group.events.length }} 个事件</span>
            </div>

            <article v-for="event in group.events" :key="event.id" class="event-card">
              <div class="event-kicker">
                <span class="importance">{{ event.importance.toFixed(1) }}</span>
                <span>{{ event.source_count }} 家独立来源</span>
                <span>
                  {{ event.time_basis === 'observed' ? '热榜抓取' : '发布' }}
                  {{ formatTimestamp(event.published_at) }}
                </span>
              </div>
              <h3>{{ event.title }}</h3>
              <div
                v-if="event.original_title && event.original_title !== event.title"
                class="original-title"
              >
                {{ event.original_title }}
              </div>
              <p class="event-summary">{{ event.summary }}</p>
              <div v-if="event.why_it_matters" class="why-row">
                <strong>为何重要</strong>
                <span>{{ event.why_it_matters }}</span>
              </div>
              <div class="source-links">
                <a
                  v-for="source in event.sources"
                  :key="source.article_id"
                  :href="source.url"
                  target="_blank"
                  rel="noopener noreferrer"
                  :title="source.title"
                >
                  <LinkOutlined />
                  {{ source.source_name || source.source_domain }}
                </a>
              </div>
            </article>
          </section>

          <footer v-if="digest.source_stats.attempted" class="coverage-note">
            本轮检查 {{ digest.source_stats.attempted }} 个信源，
            成功 {{ digest.source_stats.succeeded || 0 }} 个，
            读取 {{ digest.source_stats.articles || 0 }} 条候选报道。
          </footer>
        </template>
      </div>
    </main>

    <NewsChatPanel class="desktop-chat" />

    <a-drawer
      v-model:open="chatOpen"
      placement="right"
      :width="'min(420px, 94vw)'"
      :closable="false"
      :body-style="{ padding: 0 }"
      root-class-name="news-chat-drawer"
      @after-open-change="onDrawerOpenChange"
    >
      <div class="drawer-head">
        <span>新闻追问</span>
        <button
          ref="drawerCloseButton"
          type="button"
          class="drawer-close"
          aria-label="关闭新闻追问"
          @click="chatOpen = false"
        >
          <CloseOutlined aria-hidden="true" />
        </button>
      </div>
      <NewsChatPanel embedded />
    </a-drawer>
  </div>
</template>

<style scoped>
.news-shell {
  display: grid;
  grid-template-columns: 264px minmax(0, 1fr) 360px;
  height: calc(100vh - 48px);
  overflow: hidden;
  background: var(--surface-muted);
}

.news-filters {
  border-inline-end: 1px solid var(--border);
}

.filters-body {
  padding: 14px;
}

.filter-lock {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 12px;
  padding: 8px 9px;
  border: 1px solid color-mix(in srgb, var(--accent) 20%, var(--border));
  border-radius: 8px;
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 6%, var(--surface));
  font-size: 10px;
  font-weight: 550;
}

.filter-section + .filter-section {
  margin-top: 22px;
}

.section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 9px;
  color: var(--text);
  font-size: 12px;
  font-weight: 650;
}

.section-note {
  color: var(--text-muted);
  font-size: 10px;
  font-weight: 400;
}

.category-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  margin: -3px 0 9px;
}

.category-actions button {
  padding: 0;
  border: 0;
  color: var(--text-muted);
  background: transparent;
  cursor: pointer;
  font: inherit;
  font-size: 10px;
}

.category-actions button:hover {
  color: var(--accent);
}

.category-actions button:disabled {
  cursor: wait;
  opacity: 0.55;
}

.filter-error {
  margin-bottom: 10px;
  padding: 8px 9px;
  border: 1px solid #fde68a;
  border-radius: 8px;
  color: #92400e;
  background: #fffbeb;
  font-size: 10px;
  line-height: 1.55;
}

.category-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
}

.category-option {
  display: flex;
  min-width: 0;
  min-height: 38px;
  align-items: center;
  justify-content: space-between;
  gap: 5px;
  padding: 8px 9px;
  border: 1px solid var(--border);
  border-radius: 9px;
  color: var(--text);
  background: var(--surface);
  cursor: pointer;
  font: inherit;
  text-align: start;
  transition: background-color 140ms ease, border-color 140ms ease, scale 100ms ease;
}

.category-option:hover {
  border-color: color-mix(in srgb, var(--accent) 28%, var(--border));
  background: var(--surface-muted);
}

.category-option:active {
  scale: 0.97;
}

.category-option:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--accent) 55%, transparent);
  outline-offset: 1px;
}

.category-option.selected {
  border-color: color-mix(in srgb, var(--accent) 48%, var(--border));
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 7%, var(--surface));
}

.category-option:disabled {
  cursor: not-allowed;
  opacity: 0.48;
  filter: grayscale(0.35);
}

.category-name {
  overflow: hidden;
  font-size: 12px;
  font-weight: 550;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.category-check {
  display: grid;
  width: 15px;
  height: 15px;
  flex: 0 0 15px;
  place-items: center;
  border: 1px solid var(--border);
  border-radius: 5px;
  color: #fff;
  background: var(--surface);
  font-size: 9px;
}

.category-option.selected .category-check {
  border-color: var(--accent);
  background: var(--accent);
}

.custom-input-row {
  display: flex;
  gap: 6px;
}

.topic-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 9px;
}

.date-control {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  padding: 7px 9px;
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--text-muted);
  background: var(--surface);
}

.date-control input {
  width: 100%;
  min-width: 0;
  border: 0;
  outline: none;
  color: var(--text);
  background: transparent;
  font: inherit;
  font-size: 12px;
}

.date-control:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 18%, transparent);
}

.date-control.disabled {
  cursor: wait;
  opacity: 0.65;
  background: var(--surface-muted);
}

.schedule-note {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-top: 20px;
  padding: 10px;
  border-radius: 9px;
  color: var(--text-muted);
  background: var(--surface-muted);
  font-size: 10px;
  line-height: 1.6;
}

.schedule-note :deep(svg) {
  flex: 0 0 auto;
  margin-top: 2px;
  color: var(--warning);
}

.news-feed {
  min-width: 0;
}

.feed-header {
  min-height: 64px;
}

.feed-title {
  margin: 0;
  font-size: 15px;
  font-weight: 650;
  overflow-wrap: anywhere;
  text-wrap: balance;
}

.feed-subtitle {
  margin-top: 3px;
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 400;
}

.mobile-chat-trigger {
  display: none;
}

.feed-body {
  padding: 22px clamp(18px, 3vw, 38px) 40px;
}

.feed-state {
  display: flex;
  min-height: 55vh;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  gap: 10px;
  color: var(--text-muted);
  font-size: 13px;
  text-align: center;
}

.feed-state.compact {
  min-height: 34vh;
}

.feed-state strong {
  color: var(--text);
  font-size: 16px;
}

.state-icon,
.pulse-mark {
  display: grid;
  width: 46px;
  height: 46px;
  place-items: center;
  border-radius: 14px;
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 9%, var(--surface));
  font-size: 21px;
}

.processing-state .pulse-mark {
  animation: breathe 1.8s ease-in-out infinite;
}

@keyframes breathe {
  50% { scale: 1.04; opacity: 0.72; }
}

.warning-strip {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  margin-bottom: 20px;
  padding: 10px 12px;
  border: 1px solid #fde68a;
  border-radius: 9px;
  color: #92400e;
  background: #fffbeb;
  font-size: 11px;
  line-height: 1.55;
}

.warning-strip div {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.event-group + .event-group {
  margin-top: 32px;
}

.group-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 11px;
}

.group-heading h2 {
  margin: 0;
  font-size: 17px;
  letter-spacing: -0.01em;
  overflow-wrap: anywhere;
  text-wrap: balance;
}

.group-heading span {
  color: var(--text-muted);
  font-size: 10px;
}

.event-card {
  padding: 18px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface);
  box-shadow: 0 1px 2px rgb(15 23 42 / 0.03);
}

.event-card + .event-card {
  margin-top: 11px;
}

.event-card:hover {
  box-shadow: 0 7px 22px rgb(15 23 42 / 0.055);
}

.event-kicker {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 9px;
  color: var(--text-muted);
  font-size: 10px;
}

.importance {
  display: inline-flex;
  min-width: 34px;
  justify-content: center;
  padding: 2px 7px;
  border-radius: 999px;
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 9%, var(--surface));
  font-variant-numeric: tabular-nums;
  font-weight: 650;
}

.event-card h3 {
  margin: 9px 0 3px;
  font-size: 18px;
  line-height: 1.42;
  letter-spacing: -0.015em;
  overflow-wrap: anywhere;
  text-wrap: balance;
}

.original-title {
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.event-summary {
  margin: 10px 0 0;
  color: #374151;
  font-size: 13px;
  line-height: 1.75;
  overflow-wrap: anywhere;
  text-wrap: pretty;
}

.why-row {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 9px;
  margin-top: 12px;
  padding: 9px 11px;
  border-radius: 8px;
  color: #4b5563;
  background: var(--surface-muted);
  font-size: 11px;
  line-height: 1.6;
}

.why-row strong {
  color: var(--text);
}

.why-row span {
  min-width: 0;
  overflow-wrap: anywhere;
}

.source-links {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 13px;
}

.source-links a {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 190px;
  padding: 4px 8px;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--text-muted);
  font-size: 10px;
  text-decoration: none;
  text-overflow: ellipsis;
  white-space: nowrap;
  transition: color 140ms ease, border-color 140ms ease;
}

.source-links a:hover {
  border-color: color-mix(in srgb, var(--accent) 35%, var(--border));
  color: var(--accent);
}

.coverage-note {
  margin-top: 30px;
  padding-top: 14px;
  border-top: 1px solid var(--border);
  color: var(--text-muted);
  font-size: 10px;
  text-align: center;
}

.desktop-chat {
  min-width: 0;
}

.drawer-head {
  display: flex;
  height: 48px;
  align-items: center;
  justify-content: space-between;
  padding: 0 12px 0 16px;
  border-bottom: 1px solid var(--border);
  font-size: 14px;
  font-weight: 650;
}

.drawer-close {
  display: grid;
  width: 28px;
  height: 28px;
  place-items: center;
  border: 0;
  border-radius: 7px;
  color: var(--text-muted);
  background: transparent;
  cursor: pointer;
  font: inherit;
}

.drawer-close:hover {
  color: var(--text);
  background: var(--surface-muted);
}

.drawer-close:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

@media (max-width: 1280px) {
  .news-shell {
    grid-template-columns: 240px minmax(0, 1fr) 320px;
  }
}

@media (max-width: 1060px) {
  .news-shell {
    grid-template-columns: 230px minmax(0, 1fr);
  }

  .desktop-chat {
    display: none;
  }

  .mobile-chat-trigger {
    display: inline-flex;
  }
}

@media (max-width: 760px) {
  .news-shell {
    display: block;
    overflow-y: auto;
  }

  .news-filters {
    border-inline-end: 0;
    border-bottom: 1px solid var(--border);
  }

  .filters-body {
    max-height: none;
  }

  .category-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .news-feed {
    min-height: 70vh;
  }

  .feed-body {
    padding: 18px 14px 32px;
  }

  .feed-header {
    position: sticky;
    top: 0;
    z-index: 2;
  }
}

@media (prefers-reduced-motion: reduce) {
  .processing-state .pulse-mark {
    animation: none;
  }
  .category-option,
  .source-links a {
    transition: none;
  }
}
</style>
