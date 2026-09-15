<script setup lang="ts">
import { computed, ref } from 'vue'

/**
 * Collapsible reasoning panel for one assistant turn.
 *
 * The backend streams reasoning fragments as `thinking` events and never persists
 * them, so this block only exists while the turn is in memory. While fragments are
 * still arriving the toggle reads "模型思考中"; afterwards it reads as a reviewable
 * record of the deliberation.
 */
const props = defineProps<{ text: string; active?: boolean }>()

const expanded = ref(false)
const label = computed(() => (props.active ? '模型思考中…' : '查看思考过程'))
</script>

<template>
  <div class="thinking-block">
    <button type="button" class="thinking-toggle" @click="expanded = !expanded">
      <span v-if="active" class="pulse" />
      <span class="thinking-label">{{ label }}</span>
      <span class="thinking-action">{{ expanded ? '收起' : '展开' }}</span>
    </button>
    <pre v-if="expanded" class="thinking-text">{{ text }}</pre>
  </div>
</template>

<style scoped>
.thinking-block {
  margin-bottom: 10px;
}

.thinking-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  border: 1px dashed var(--border);
  background: transparent;
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 12px;
  color: var(--text-muted);
  cursor: pointer;
}

.thinking-toggle:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.pulse {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  animation: thinking-pulse 1.2s ease-in-out infinite;
}

@keyframes thinking-pulse {
  0%,
  100% {
    opacity: 0.3;
  }
  50% {
    opacity: 1;
  }
}

.thinking-label {
  flex: 1;
  text-align: left;
}

.thinking-action {
  font-size: 11px;
  opacity: 0.8;
}

.thinking-text {
  margin: 6px 0 0;
  padding: 10px 12px;
  max-height: 220px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-muted);
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
}
</style>
