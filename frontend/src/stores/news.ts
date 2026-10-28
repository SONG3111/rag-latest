import { defineStore } from 'pinia'
import { api } from '@/api'
import { streamNewsChat } from '@/composables/useChatStream'
import type {
  NewsCategory,
  NewsDigest,
  NewsHealth,
  NewsMessage,
  NewsTurn,
} from '@/types'

let counter = 0
const nextId = (prefix: string) => `${prefix}-${Date.now()}-${(counter += 1)}`
const STORAGE_KEY = 'news-radar-category-selection'
const BUILTIN_CATEGORIES: NewsCategory[] = [
  { id: 'technology', label: '科技', description: '人工智能、芯片、软件、互联网与前沿技术' },
  { id: 'finance', label: '财经', description: '市场、央行、公司财报与宏观经济' },
  { id: 'world', label: '政经国际', description: '地缘政治、选举、冲突与国际政策' },
  { id: 'society', label: '社会', description: '公共安全、人口、劳动与社会政策' },
  { id: 'health', label: '健康', description: '公共卫生、医学研究、药品与医疗改革' },
  { id: 'education', label: '教育', description: '学校、招生、科研与教育政策' },
  { id: 'culture_sports', label: '文体娱乐', description: '体育赛事、影视、音乐与文化事件' },
  { id: 'auto', label: '汽车', description: '车企、车型、智能驾驶、召回与产业政策' },
  { id: 'energy', label: '能源', description: '油气、电力、新能源、矿产与能源安全' },
  { id: 'consumer', label: '消费', description: '零售、电商、消费品牌与服务趋势' },
]
const DEFAULT_CATEGORIES = ['technology', 'finance', 'world']

function shanghaiDate(): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date())
  const value = (type: string) => parts.find((part) => part.type === type)?.value || ''
  return `${value('year')}-${value('month')}-${value('day')}`
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

function storedSelection(): { categories: string[]; custom_topics: string[] } | null {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null')
    if (
      parsed &&
      Array.isArray(parsed.categories) &&
      Array.isArray(parsed.custom_topics)
    ) {
      return {
        categories: parsed.categories.map(String),
        custom_topics: parsed.custom_topics.map(String),
      }
    }
  } catch {
    /* local fallback is optional */
  }
  return null
}

export const useNewsStore = defineStore('news', {
  state: () => ({
    categories: BUILTIN_CATEGORIES,
    defaults: [...DEFAULT_CATEGORIES],
    customTopicLimit: 3,
    selectedCategories: [] as string[],
    customTopics: [] as string[],
    digest: null as NewsDigest | null,
    health: null as NewsHealth | null,
    selectedDate: shanghaiDate(),
    loading: false,
    refreshing: false,
    savingPreferences: false,
    error: null as string | null,
    turns: [] as NewsTurn[],
    streaming: false,
    streamController: null as AbortController | null,
  }),

  getters: {
    hasSelection(state): boolean {
      return state.selectedCategories.length > 0 || state.customTopics.length > 0
    },
    hasProcessingDigest(state): boolean {
      return state.digest?.status === 'pending' || state.digest?.status === 'running'
    },
  },

  actions: {
    async bootstrap() {
      this.loading = true
      this.error = null
      const local = storedSelection()
      this.selectedCategories = local?.categories || [...this.defaults]
      this.customTopics = local?.custom_topics || []
      const [categories, preference, health, messages] = await Promise.allSettled([
        api.newsCategories(),
        api.newsPreferences(),
        api.newsHealth(),
        api.newsMessages(),
      ])
      if (categories.status === 'fulfilled') {
        this.categories = categories.value.categories
        this.defaults = categories.value.defaults
        this.customTopicLimit = categories.value.custom_topic_limit
      } else {
        this.categories = BUILTIN_CATEGORIES
        this.defaults = [...DEFAULT_CATEGORIES]
        this.error =
          '新闻后端尚未就绪，关注行业已使用本地内置列表；请重启后端后再生成新闻。'
      }
      if (preference.status === 'fulfilled') {
        this.selectedCategories = preference.value.categories
        this.customTopics = preference.value.custom_topics
        this.persistSelectionLocal()
      }
      if (health.status === 'fulfilled') this.health = health.value
      if (messages.status === 'fulfilled') {
        this.turns = this.mapMessages(messages.value)
      }
      await this.loadDigest(this.selectedDate)
      this.loading = false
    },

    persistSelectionLocal() {
      try {
        localStorage.setItem(
          STORAGE_KEY,
          JSON.stringify({
            categories: this.selectedCategories,
            custom_topics: this.customTopics,
          }),
        )
      } catch {
        /* local fallback is optional */
      }
    },

    mapMessages(messages: NewsMessage[]): NewsTurn[] {
      return messages.map((message) => ({
        id: message.id,
        role: message.role === 'user' ? 'user' : 'assistant',
        content: message.content,
        activities: [],
        streaming: false,
        messageId: message.id,
      }))
    },

    async loadDigest(date?: string) {
      const targetDate = date || this.selectedDate
      this.selectedDate = targetDate
      try {
        this.digest = await api.latestNewsDigest(targetDate)
      } catch (error) {
        const text = error instanceof Error ? error.message : String(error)
        this.digest = null
        if (!text.includes('404')) this.error = text
      }
    },

    async savePreferences() {
      this.savingPreferences = true
      this.persistSelectionLocal()
      if (!this.hasSelection) {
        this.savingPreferences = false
        return
      }
      try {
        const saved = await api.updateNewsPreferences(
          this.selectedCategories,
          this.customTopics,
        )
        this.selectedCategories = saved.categories
        this.customTopics = saved.custom_topics
        this.persistSelectionLocal()
        this.error = null
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error)
        this.error =
          detail.includes('404') || detail.includes('Not Found')
            ? '后端新闻接口尚未就绪，当前选择已保存在本地；请重启后端后再生成新闻。'
            : detail
      } finally {
        this.savingPreferences = false
      }
    },

    async toggleCategory(categoryId: string) {
      if (this.refreshing || this.hasProcessingDigest) return
      const selected = new Set(this.selectedCategories)
      if (selected.has(categoryId)) selected.delete(categoryId)
      else selected.add(categoryId)
      this.selectedCategories = this.categories
        .map((category) => category.id)
        .filter((id) => selected.has(id))
      await this.savePreferences()
    },

    async selectAllCategories() {
      if (this.refreshing || this.hasProcessingDigest) return
      this.selectedCategories = this.categories.map((category) => category.id)
      await this.savePreferences()
    },

    async resetDefaultCategories() {
      if (this.refreshing || this.hasProcessingDigest) return
      this.selectedCategories = this.defaults.filter((id) =>
        this.categories.some((category) => category.id === id),
      )
      await this.savePreferences()
    },

    async addCustomTopic(raw: string) {
      if (this.refreshing || this.hasProcessingDigest) return
      const topic = raw.replace(/\s+/g, ' ').trim()
      if (topic.length < 2 || topic.length > 40) {
        throw new Error('自定义主题需要 2–40 个字符')
      }
      if (this.customTopics.length >= this.customTopicLimit) {
        throw new Error(`最多只能添加 ${this.customTopicLimit} 个自定义主题`)
      }
      if (this.customTopics.some((item) => item.toLowerCase() === topic.toLowerCase())) {
        return
      }
      this.customTopics = [...this.customTopics, topic]
      await this.savePreferences()
    },

    async removeCustomTopic(topic: string) {
      if (this.refreshing || this.hasProcessingDigest) return
      this.customTopics = this.customTopics.filter((item) => item !== topic)
      await this.savePreferences()
    },

    async refreshDigest() {
      if (this.refreshing || !this.hasSelection) return
      this.refreshing = true
      this.error = null
      try {
        await this.savePreferences()
        let digest = await api.refreshNewsDigest()
        this.digest = digest
        this.selectedDate = digest.edition_date
        for (let attempt = 0; attempt < 120; attempt += 1) {
          if (digest.status === 'ready' || digest.status === 'failed') break
          await sleep(1500)
          digest = await api.getNewsDigest(digest.id)
          this.digest = digest
        }
        if (digest.status === 'pending' || digest.status === 'running') {
          throw new Error('新闻生成仍在后台进行，请稍后重新打开页面查看')
        }
        if (digest.status === 'failed') {
          throw new Error(digest.error || '新闻生成失败')
        }
        await this.refreshHealth()
      } catch (error) {
        this.error = error instanceof Error ? error.message : String(error)
        throw error
      } finally {
        this.refreshing = false
      }
    },

    async refreshHealth() {
      try {
        this.health = await api.newsHealth()
      } catch {
        // Health is informational and must not hide the digest.
      }
    },

    async send(message: string) {
      if (this.streaming) return
      this.turns.push({
        id: nextId('news-user'),
        role: 'user',
        content: message,
        activities: [],
        streaming: false,
      })
      const assistantId = nextId('news-assistant')
      this.turns.push({
        id: assistantId,
        role: 'assistant',
        content: '',
        activities: [],
        streaming: true,
      })
      this.streaming = true
      this.error = null
      const controller = new AbortController()
      this.streamController = controller
      const turn = () => this.turns.find((item) => item.id === assistantId)
      try {
        await streamNewsChat(
          message,
          {
            onToken: (payload) => {
              const item = turn()
              if (item) item.content += payload.text
            },
            onToolCall: (payload) => {
              turn()?.activities.push({
                id: nextId('news-activity'),
                tool: payload.tool,
                label: payload.label,
                status: 'running',
              })
            },
            onToolResult: () => {
              const item = turn()
              const running = [...(item?.activities || [])]
                .reverse()
                .find((activity) => activity.status === 'running')
              if (running) running.status = 'done'
            },
            onDigest: (payload) => {
              this.digest = payload.digest
              this.selectedDate = payload.digest.edition_date
              const item = turn()
              if (!item) return
              const existing = item.activities.find(
                (activity) => activity.tool === 'generate_news_digest',
              )
              if (payload.digest.status === 'pending' || payload.digest.status === 'running') {
                if (!existing) {
                  item.activities.push({
                    id: nextId('news-activity'),
                    tool: 'generate_news_digest',
                    label: '按当前行业生成新闻快照',
                    status: 'running',
                  })
                }
              } else if (existing) {
                existing.status = 'done'
              }
            },
            onDone: (payload) => {
              const item = turn()
              if (!item) return
              if (payload.content) item.content = payload.content
              if (payload.message_id) item.messageId = payload.message_id
            },
            onError: (payload) => {
              const item = turn()
              if (item) item.error = payload.message
            },
          },
          controller.signal,
        )
      } catch (error) {
        const item = turn()
        const aborted = error instanceof DOMException && error.name === 'AbortError'
        if (aborted) {
          if (item?.content) item.content += '\n\n（已停止生成，以上为已生成的部分。）'
        } else {
          const text = error instanceof Error ? error.message : String(error)
          if (item) item.error = text
          this.error = text
        }
      } finally {
        const item = turn()
        if (item) item.streaming = false
        this.streaming = false
        this.streamController = null
      }
    },

    stop() {
      this.streamController?.abort()
      this.streamController = null
    },
  },
})
