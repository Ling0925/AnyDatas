<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { useTheme } from '../theme'

const { isDark } = useTheme()
const canvasRef = ref<HTMLCanvasElement | null>(null)

const LIGHT_WAVE_COLORS = [
  '#147d64',
  '#48a08a',
  '#79b9a9',
] as const
const DARK_WAVE_COLORS = [
  '#59c9a6',
  '#3eab8d',
  '#277e6b',
] as const
const waveColors = computed(() => (
  isDark.value ? DARK_WAVE_COLORS : LIGHT_WAVE_COLORS
))

const MIN_TIDE_DEPTH = 0.28
const MAX_TIDE_DEPTH = 0.46
const FRAME_INTERVAL = 1000 / 60

let context: CanvasRenderingContext2D | null = null
let resizeObserver: ResizeObserver | null = null
let motionPreference: MediaQueryList | null = null
let animationFrameId = 0
let canvasWidth = 0
let canvasHeight = 0
let canvasScale = 1
let pixelStep = 7
let gridColumnCount = 0
let gridStartRow = 0
let gridRowCount = 0
let lastAnimationAt = 0
let renderAccumulator = 0
let simulationElapsed = 0
let nextTideAt = 0
let currentTideDepth = 0.34
let targetTideDepth = 0.38
let phaseFieldA = new Float32Array(0)
let phaseFieldB = new Float32Array(0)
let phaseFieldC = new Float32Array(0)
let thresholdNoise = new Float32Array(0)
let spatialBias = new Float32Array(0)

const patternSeed = Math.random() * 10_000
const wavePhaseA = Math.random() * Math.PI * 2
const wavePhaseB = Math.random() * Math.PI * 2
const wavePhaseC = Math.random() * Math.PI * 2

/**
 * 将数值约束在安全区间内。
 * 为什么这么做：长帧和尺寸变化可能产生超出预期的中间值；
 * 好处：潮位、阈值与透明度始终稳定，不会造成突变或越界。
 *
 * @param value 待约束的数值。
 * @param minimum 最小值。
 * @param maximum 最大值。
 * @returns 约束后的数值。
 */
function clamp(value: number, minimum: number, maximum: number) {
  return Math.min(maximum, Math.max(minimum, value))
}

/**
 * 为二维网格生成稳定伪随机值。
 * 为什么这么做：每帧重新随机会让单个像素闪烁，破坏海浪的连续感；
 * 好处：波峰会自然断成像素短簇，同时在流动过程中保持一致纹理。
 *
 * @param column 网格列索引。
 * @param row 网格行索引。
 * @returns 0 到 1 之间的稳定数值。
 */
function getCellNoise(column: number, row: number) {
  const value = Math.sin(column * 12.9898 + row * 78.233 + patternSeed) * 43_758.5453
  return value - Math.floor(value)
}

/**
 * 选择下一段随机潮位与持续时间。
 * 为什么这么做：固定周期会让二维海面像循环播放的装饰动画；
 * 好处：波峰覆盖范围会缓慢随机增减，形成不重复的涨潮与退潮。
 *
 * @param now 当前高精度时间戳。
 */
function scheduleNextTide(now: number) {
  targetTideDepth = MIN_TIDE_DEPTH + Math.random() * (MAX_TIDE_DEPTH - MIN_TIDE_DEPTH)
  nextTideAt = now + 5_500 + Math.random() * 7_500
}

/**
 * 预计算俯视海面的二维波场。
 * 为什么这么做：每帧重复计算椭圆距离与空间遮罩会浪费大量算力；
 * 好处：动画阶段只需更新三个波相位并筛选波峰，可稳定维持 60 FPS。
 */
function rebuildWaveField() {
  gridColumnCount = Math.ceil(canvasWidth / pixelStep) + 1
  gridStartRow = 0
  gridRowCount = Math.max(1, Math.ceil(canvasHeight / pixelStep) - gridStartRow + 1)

  const cellCount = gridColumnCount * gridRowCount
  phaseFieldA = new Float32Array(cellCount)
  phaseFieldB = new Float32Array(cellCount)
  phaseFieldC = new Float32Array(cellCount)
  thresholdNoise = new Float32Array(cellCount)
  spatialBias = new Float32Array(cellCount)

  const sourceBX = canvasWidth * 0.76
  const sourceBY = canvasHeight * 1.16

  for (let localRow = 0; localRow < gridRowCount; localRow += 1) {
    const row = gridStartRow + localRow
    const y = row * pixelStep
    const yRatio = y / Math.max(1, canvasHeight)

    for (let column = 0; column < gridColumnCount; column += 1) {
      const index = localRow * gridColumnCount + column
      const x = column * pixelStep
      const xRatio = x / Math.max(1, canvasWidth)

      // 静态二维扭曲会让等高波纹呈现海面弧度，而不是规则同心圆或横向扫描线。
      const warpedX = x + Math.sin(y * 0.008 + patternSeed) * 34
      const warpedY = y + Math.sin(x * 0.006 - patternSeed * 0.7) * 28
      const distanceB = Math.hypot(
        (warpedX - sourceBX) * 1.05,
        (warpedY - sourceBY) * 0.76,
      )

      phaseFieldA[index] = (
        warpedX * 0.009
        + warpedY * 0.035
        + Math.sin(warpedX * 0.005 + patternSeed) * 0.9
        + wavePhaseA
      )
      phaseFieldB[index] = distanceB * 0.034 + wavePhaseB
      phaseFieldC[index] = (
        warpedX * 0.012
        + warpedY * 0.017
        + Math.sin((warpedX + warpedY) * 0.004) * 0.72
        + wavePhaseC
      )
      thresholdNoise[index] = getCellNoise(column, row)

      // 仅用低幅二维起伏改变局部浪量，不按上下或左右裁切，保证整块画布都属于同一片俯视海面。
      const densityDrift = (
        Math.sin(xRatio * Math.PI * 2.6 + yRatio * Math.PI * 1.7 + patternSeed)
        + Math.sin(xRatio * Math.PI * 5.1 - yRatio * Math.PI * 2.2 - patternSeed * 0.3)
        + 2
      ) * 0.25
      spatialBias[index] = densityDrift * 0.055
    }
  }
}

/**
 * 推进随机潮位。
 * 为什么这么做：波形用绝对时间移动，而潮位需要按真实帧间隔平滑追随随机目标；
 * 好处：偶发掉帧不会让涨潮速度突变，高刷屏也不会重复累计模拟时间。
 *
 * @param now 当前高精度时间戳。
 * @param deltaMs 自上次实际模拟以来的墙钟毫秒数。
 */
function updateSimulation(now: number, deltaMs: number) {
  if (now >= nextTideAt) scheduleNextTide(now)

  const tideEase = 1 - Math.exp(-deltaMs / 5_200)
  currentTideDepth += (targetTideDepth - currentTideDepth) * tideEase
}

/**
 * 绘制空中俯视的二维像素海洋。
 * 为什么这么做：每个网格只在合成波高达到波峰阈值时绘制主题色方块；
 * 好处：波浪具有清晰像素感，而所有平静网格从未着色，Canvas 底层保持真正透明。
 *
 * @param elapsedSeconds 页面动画已经运行的秒数。
 */
function paintPixelOcean(elapsedSeconds: number) {
  if (!context || canvasWidth <= 0 || canvasHeight <= 0) return

  // 每帧先清空透明画布，且只为达到波峰阈值的网格着色，平静区域不会留下任何底色。
  context.clearRect(0, 0, canvasWidth, canvasHeight)

  const activeWaveColors = waveColors.value
  const crestPaths = activeWaveColors.map(() => new Path2D())
  const tideRatio = clamp(
    (currentTideDepth - MIN_TIDE_DEPTH) / (MAX_TIDE_DEPTH - MIN_TIDE_DEPTH),
    0,
    1,
  )
  const baseThreshold = 0.8 - tideRatio * 0.1
  const cellInset = 1 / canvasScale

  for (let localRow = 0; localRow < gridRowCount; localRow += 1) {
    const row = gridStartRow + localRow
    const y = row * pixelStep

    for (let column = 0; column < gridColumnCount; column += 1) {
      const index = localRow * gridColumnCount + column
      const fieldWarp = Math.sin(
        phaseFieldC[index] - elapsedSeconds * 0.18,
      )
      const primaryCrest = Math.cos(
        phaseFieldA[index]
        - elapsedSeconds * 1.08
        + Math.sin(phaseFieldB[index] + elapsedSeconds * 0.22) * 0.62
        + fieldWarp * 0.22,
      )
      const secondaryCrest = Math.cos(
        phaseFieldB[index]
        + elapsedSeconds * 0.64
        + fieldWarp * 0.34,
      ) * 0.78
      const clusterAmplitude = 0.82 + fieldWarp * 0.18
      const waveHeight = Math.max(primaryCrest * clusterAmplitude, secondaryCrest)
      const threshold = (
        baseThreshold
        + spatialBias[index]
        + (thresholdNoise[index] - 0.5) * 0.12
      )
      if (waveHeight <= threshold) continue

      const crestStrength = clamp((waveHeight - threshold) / 0.34, 0, 1)
      const colorIndex = crestStrength > 0.58 ? 0 : crestStrength > 0.24 ? 1 : 2
      const pixelSize = colorIndex === 0 ? pixelStep - 1 : pixelStep - 2
      const x = column * pixelStep + (pixelStep - pixelSize) * 0.5
      const centeredY = y + (pixelStep - pixelSize) * 0.5
      const pixelLeft = Math.round(x * canvasScale) / canvasScale + cellInset
      const pixelTop = Math.round(centeredY * canvasScale) / canvasScale + cellInset
      const renderedSize = Math.max(1, pixelSize - cellInset * 2)

      crestPaths[colorIndex].rect(pixelLeft, pixelTop, renderedSize, renderedSize)
    }
  }

  const colorOpacity = [0.96, 0.78, 0.58]
  for (let colorIndex = 0; colorIndex < activeWaveColors.length; colorIndex += 1) {
    context.fillStyle = activeWaveColors[colorIndex]
    context.globalAlpha = colorOpacity[colorIndex]
    context.fill(crestPaths[colorIndex])
  }
  context.globalAlpha = 1
}

/**
 * 根据容器尺寸同步 Canvas 和二维网格。
 * 为什么这么做：CSS 拉伸会模糊方块边缘，DPR 变化也会让像素尺寸失真；
 * 好处：桌面与移动端都能获得对齐物理像素的清晰方格，并限制高分屏绘制成本。
 */
function resizeCanvas() {
  const canvas = canvasRef.value
  if (!canvas) return

  const bounds = canvas.getBoundingClientRect()
  const nextWidth = Math.max(1, Math.floor(bounds.width))
  const nextHeight = Math.max(1, Math.floor(bounds.height))
  const nextPixelStep = nextWidth < 760 ? 6 : 7
  const nextScale = Math.min(window.devicePixelRatio || 1, 2)
  const fieldChanged = (
    nextWidth !== canvasWidth
    || nextHeight !== canvasHeight
    || nextPixelStep !== pixelStep
    || nextScale !== canvasScale
  )

  canvasWidth = nextWidth
  canvasHeight = nextHeight
  canvasScale = nextScale
  pixelStep = nextPixelStep

  const backingWidth = Math.floor(canvasWidth * canvasScale)
  const backingHeight = Math.floor(canvasHeight * canvasScale)
  if (canvas.width !== backingWidth) canvas.width = backingWidth
  if (canvas.height !== backingHeight) canvas.height = backingHeight

  context = canvas.getContext('2d', { alpha: true })
  context?.setTransform(canvasScale, 0, 0, canvasScale, 0, 0)
  if (context) context.imageSmoothingEnabled = false

  if (fieldChanged) rebuildWaveField()
  paintPixelOcean(performance.now() / 1_000)
}

/**
 * 以 60 FPS 上限驱动二维海面。
 * 为什么这么做：绘制节流相位与实际模拟耗时必须分开，否则高刷屏会重复计算余量；
 * 好处：60Hz 屏逐帧更新，90/120/144Hz 屏保持约 60 次绘制且潮位速度不漂移。
 *
 * @param now 浏览器提供的当前高精度时间戳。
 */
function animate(now: number) {
  if (lastAnimationAt === 0) lastAnimationAt = now
  const wallDelta = clamp(now - lastAnimationAt, 0, 34)
  lastAnimationAt = now
  renderAccumulator += wallDelta
  simulationElapsed += wallDelta

  if (renderAccumulator >= FRAME_INTERVAL * 0.9) {
    updateSimulation(now, simulationElapsed)
    paintPixelOcean(now / 1_000)
    simulationElapsed = 0
    renderAccumulator = renderAccumulator >= FRAME_INTERVAL
      ? renderAccumulator % FRAME_INTERVAL
      : 0
  }

  animationFrameId = window.requestAnimationFrame(animate)
}

/**
 * 在页面可见且允许动态效果时启动动画。
 * 为什么这么做：重复启动会叠加 requestAnimationFrame 循环；
 * 好处：始终只有一条动画链，路由切换与偏好变化时容易安全回收。
 */
function startAnimation() {
  if (animationFrameId || motionPreference?.matches || document.hidden) return

  const now = performance.now()
  if (nextTideAt <= now) scheduleNextTide(now)
  lastAnimationAt = 0
  renderAccumulator = 0
  simulationElapsed = 0
  animationFrameId = window.requestAnimationFrame(animate)
}

/**
 * 停止并清理当前动画帧。
 * 为什么这么做：隐藏或卸载后继续绘制没有用户价值；
 * 好处：避免后台耗电，也不会在组件销毁后继续访问 Canvas。
 */
function stopAnimation() {
  if (animationFrameId) window.cancelAnimationFrame(animationFrameId)
  animationFrameId = 0
  lastAnimationAt = 0
  renderAccumulator = 0
  simulationElapsed = 0
}

/**
 * 响应页面可见性变化。
 * 为什么这么做：后台标签页不需要持续更新二维波场；
 * 好处：离开页面时暂停，回来后从真实时间安全续播。
 */
function handleVisibilityChange() {
  if (document.hidden) {
    stopAnimation()
    return
  }
  startAnimation()
}

/**
 * 响应用户的减少动态效果设置。
 * 为什么这么做：持续波动可能让对运动敏感的用户不适；
 * 好处：开启后保留一帧像素海洋，但不再发生任何移动或潮位变化。
 */
function handleMotionPreferenceChange() {
  stopAnimation()

  if (motionPreference?.matches) {
    currentTideDepth = 0.34
    targetTideDepth = 0.34
    paintPixelOcean(performance.now() / 1_000)
    return
  }

  scheduleNextTide(performance.now())
  startAnimation()
}

/**
 * 初始化 Canvas、尺寸监听与动态偏好。
 * 为什么这么做：浏览器资源需要从统一入口创建；
 * 好处：组件卸载时可以逐一释放，反复进入登录页也不会累积监听器。
 */
function initializeOcean() {
  const canvas = canvasRef.value
  if (!canvas) return

  motionPreference = window.matchMedia('(prefers-reduced-motion: reduce)')
  motionPreference.addEventListener('change', handleMotionPreferenceChange)
  document.addEventListener('visibilitychange', handleVisibilityChange)
  window.addEventListener('resize', resizeCanvas, { passive: true })

  resizeObserver = new ResizeObserver(resizeCanvas)
  resizeObserver.observe(canvas)
  resizeCanvas()
  startAnimation()
}

/**
 * 释放 Canvas 动画与浏览器监听器。
 * 为什么这么做：登录成功后组件会被路由卸载；
 * 好处：工作台不再承担登录背景的绘制、尺寸监听和偏好监听成本。
 */
function disposeOcean() {
  stopAnimation()
  resizeObserver?.disconnect()
  motionPreference?.removeEventListener('change', handleMotionPreferenceChange)
  document.removeEventListener('visibilitychange', handleVisibilityChange)
  window.removeEventListener('resize', resizeCanvas)
  resizeObserver = null
  motionPreference = null
  context = null
}

// 主题变化时立即重绘当前波峰，无需等待下一次动画帧，减少动态偏好模式下的视觉延迟。
watch(isDark, () => paintPixelOcean(performance.now() / 1_000))

onMounted(initializeOcean)
onBeforeUnmount(disposeOcean)
</script>

<template>
  <canvas ref="canvasRef" class="pixel-ocean" aria-hidden="true" />
</template>

<style scoped>
.pixel-ocean {
  display: block;
  width: 100%;
  height: 100%;
  pointer-events: none;
}
</style>
