<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'

const canvasRef = ref<HTMLCanvasElement | null>(null)

const OCEAN_COLORS = [
  '#c8e2db',
  '#acd3c9',
  '#79b9a9',
  '#48a08a',
  '#147d64',
  '#106b56',
] as const

const MIN_TIDE_DEPTH = 0.28
const MAX_TIDE_DEPTH = 0.46
const FRAME_INTERVAL = 1000 / 30

let context: CanvasRenderingContext2D | null = null
let resizeObserver: ResizeObserver | null = null
let motionPreference: MediaQueryList | null = null
let animationFrameId = 0
let canvasWidth = 0
let canvasHeight = 0
let canvasScale = 1
let pixelSize = 16
let lastFrameAt = 0
let nextTideAt = 0
let nextRippleAt = 0
let currentTideDepth = 0.34
let targetTideDepth = 0.38
let surfaceOffsets = new Float32Array(0)
let surfaceVelocities = new Float32Array(0)
let patternSeed = Math.random() * 10_000
const swellPhaseA = Math.random() * Math.PI * 2
const swellPhaseB = Math.random() * Math.PI * 2

/**
 * 将数值约束在安全区间内。
 * 为什么这么做：弹簧链在窗口尺寸变化或长帧后可能产生瞬时尖峰；
 * 好处：可以避免水面越界，同时让动画恢复过程保持稳定。
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
 * 为指定像素生成稳定的伪随机值。
 * 为什么这么做：每帧直接调用随机数会导致像素闪烁；
 * 好处：既能保留海面的颗粒差异，又能让同一格像素在动画中保持连续。
 *
 * @param column 像素列索引。
 * @param row 像素行索引。
 * @returns 0 到 1 之间的稳定数值。
 */
function getCellNoise(column: number, row: number) {
  const value = Math.sin(column * 12.9898 + row * 78.233 + patternSeed) * 43_758.5453
  return value - Math.floor(value)
}

/**
 * 按当前画布宽度重新创建水面弹簧链。
 * 为什么这么做：像素列数量会随窗口宽度变化，旧数组不能正确映射新列；
 * 好处：缩放窗口后仍能保持整齐的像素尺寸，不会留下拉伸或越界数据。
 */
function rebuildSurface() {
  const columnCount = Math.max(1, Math.ceil(canvasWidth / pixelSize) + 1)
  surfaceOffsets = new Float32Array(columnCount)
  surfaceVelocities = new Float32Array(columnCount)
  patternSeed = Math.random() * 10_000
}

/**
 * 选择下一次潮位目标与变化时间。
 * 为什么这么做：固定正弦波容易显得机械，随机目标更接近自然涨落；
 * 好处：每次进入登录页都能看到节奏不同、但变化缓慢的潮汐。
 *
 * @param now 当前高精度时间戳。
 */
function scheduleNextTide(now: number) {
  targetTideDepth = MIN_TIDE_DEPTH + Math.random() * (MAX_TIDE_DEPTH - MIN_TIDE_DEPTH)
  nextTideAt = now + 6_000 + Math.random() * 7_000
}

/**
 * 向随机水面位置注入一个局部波动。
 * 为什么这么做：只改变全局潮位会让水面过于平直；
 * 好处：局部脉冲经相邻弹簧传播后，会形成不重复且连贯的像素浪峰。
 *
 * @param now 当前高精度时间戳。
 */
function injectRipple(now: number) {
  if (surfaceVelocities.length === 0) return

  const center = Math.floor(Math.random() * surfaceVelocities.length)
  const radius = 3 + Math.floor(Math.random() * 6)
  const direction = Math.random() > 0.28 ? -1 : 1
  const strength = direction * pixelSize * (0.18 + Math.random() * 0.2)

  for (let offset = -radius; offset <= radius; offset += 1) {
    const index = center + offset
    if (index < 0 || index >= surfaceVelocities.length) continue

    const distanceRatio = Math.abs(offset) / (radius + 1)
    surfaceVelocities[index] += strength * (1 - distanceRatio) ** 2
  }

  nextRippleAt = now + 1_800 + Math.random() * 2_700
}

/**
 * 推进潮位与一维弹簧链模拟。
 * 为什么这么做：将全局涨潮和局部传播分开计算，可以同时获得缓慢潮汐与细小浪峰；
 * 好处：动画具有科技感但不会快速晃动，且计算量只随横向像素列增长。
 *
 * @param now 当前高精度时间戳。
 * @param deltaMs 距离上一绘制帧的毫秒数。
 */
function updateSimulation(now: number, deltaMs: number) {
  if (now >= nextTideAt) scheduleNextTide(now)
  if (now >= nextRippleAt) injectRipple(now)

  const tideEase = 1 - Math.exp(-deltaMs / 5_200)
  currentTideDepth += (targetTideDepth - currentTideDepth) * tideEase

  const frameFactor = clamp(deltaMs / 16.67, 0.35, 2)
  const damping = Math.pow(0.935, frameFactor)

  for (let index = 0; index < surfaceOffsets.length; index += 1) {
    const current = surfaceOffsets[index]
    const left = surfaceOffsets[index - 1] ?? current
    const right = surfaceOffsets[index + 1] ?? current
    const neighborForce = (left + right - current * 2) * 0.095
    const returnForce = -current * 0.018

    surfaceVelocities[index] = clamp(
      (surfaceVelocities[index] + (neighborForce + returnForce) * frameFactor) * damping,
      -pixelSize * 0.8,
      pixelSize * 0.8,
    )
  }

  for (let index = 0; index < surfaceOffsets.length; index += 1) {
    surfaceOffsets[index] = clamp(
      surfaceOffsets[index] + surfaceVelocities[index] * frameFactor,
      -pixelSize * 4.5,
      pixelSize * 4.5,
    )
  }
}

/**
 * 根据水深选择现有品牌色阶中的颜色。
 * 为什么这么做：海面与深水使用同一主色的不同明度，避免引入新的视觉语言；
 * 好处：像素海洋可以自然融入当前产品，而不是成为独立的装饰主题。
 *
 * @param depthRatio 当前像素相对于该列水深的比例。
 * @returns 对应的十六进制颜色。
 */
function getDepthColor(depthRatio: number) {
  const colorIndex = Math.min(
    OCEAN_COLORS.length - 1,
    Math.floor(clamp(depthRatio, 0, 0.999) * OCEAN_COLORS.length),
  )
  return OCEAN_COLORS[colorIndex]
}

/**
 * 将当前模拟状态绘制成带留白的像素海洋。
 * 为什么这么做：Canvas 只绘制水面以下的方格，水面以上保持真正透明；
 * 好处：页面能保留大面积白色空间，同时深度色阶仍然呈现出完整海洋。
 */
function paintOcean() {
  if (!context || canvasWidth <= 0 || canvasHeight <= 0) return

  context.clearRect(0, 0, canvasWidth, canvasHeight)
  const baseSurface = canvasHeight * (1 - currentTideDepth)
  const rowCount = Math.ceil(canvasHeight / pixelSize)
  const elapsedSeconds = performance.now() / 1_000
  const cellInset = 1 / canvasScale

  for (let column = 0; column < surfaceOffsets.length; column += 1) {
    const x = column * pixelSize
    // 两组不同尺度的慢速涌浪保证任意时刻都有可见轮廓，随机相位则避免每次进入页面都重复同一波形。
    const slowSwell = Math.sin(column * 0.034 - elapsedSeconds * 0.24 + swellPhaseA)
      * pixelSize * 1.15
    const shortSwell = Math.sin(column * 0.11 + elapsedSeconds * 0.58 + swellPhaseB)
      * pixelSize * 0.72
    const rawSurface = baseSurface + surfaceOffsets[column] + slowSwell + shortSwell
    const surface = Math.round(rawSurface / pixelSize) * pixelSize
    const startRow = Math.max(0, Math.floor(surface / pixelSize))
    const availableDepth = Math.max(pixelSize, canvasHeight - surface)
    const horizontalRatio = x / Math.max(1, canvasWidth)
    const edgeFade = clamp(1 - Math.max(0, horizontalRatio - 0.64) / 0.36, 0, 1)
    if (edgeFade <= 0.02) continue

    for (let row = startRow; row <= rowCount; row += 1) {
      const y = row * pixelSize
      const depthRatio = clamp((y - surface) / availableDepth, 0, 1)
      const noise = getCellNoise(column, row)

      // 浪尖保留少量断点，让像素轮廓更轻，不会形成厚重的整齐色带。
      if (depthRatio < 0.1 && noise < 0.16) continue

      context.fillStyle = getDepthColor(depthRatio)
      context.globalAlpha = clamp(0.34 + depthRatio * 0.24 + noise * 0.08, 0.24, 0.66)
        * edgeFade

      // 按物理像素对齐四条边，避免 1.25/1.5 等非整数 DPR 下的方格边缘被浏览器抗锯齿。
      const cellLeft = Math.round(x * canvasScale) / canvasScale + cellInset
      const cellTop = Math.round(y * canvasScale) / canvasScale + cellInset
      const cellRight = Math.round((x + pixelSize) * canvasScale) / canvasScale - cellInset
      const cellBottom = Math.round((y + pixelSize) * canvasScale) / canvasScale - cellInset
      context.fillRect(cellLeft, cellTop, cellRight - cellLeft, cellBottom - cellTop)
    }
  }

  context.globalAlpha = 1
}

/**
 * 根据容器尺寸同步 Canvas 分辨率并重绘。
 * 为什么这么做：直接使用 CSS 拉伸会让像素边缘模糊，高分屏还会出现锯齿；
 * 好处：限制 DPR 后既保持清晰像素，也避免在超高分屏上增加过多绘制成本。
 */
function resizeCanvas() {
  const canvas = canvasRef.value
  if (!canvas) return

  const bounds = canvas.getBoundingClientRect()
  const nextWidth = Math.max(1, Math.floor(bounds.width))
  const nextHeight = Math.max(1, Math.floor(bounds.height))
  const nextPixelSize = nextWidth < 760 ? 12 : 16
  const devicePixelRatio = Math.min(window.devicePixelRatio || 1, 2)
  const sizeChanged = nextWidth !== canvasWidth
    || nextHeight !== canvasHeight
    || nextPixelSize !== pixelSize

  canvasWidth = nextWidth
  canvasHeight = nextHeight
  canvasScale = devicePixelRatio
  pixelSize = nextPixelSize
  canvas.width = Math.floor(canvasWidth * devicePixelRatio)
  canvas.height = Math.floor(canvasHeight * devicePixelRatio)

  context = canvas.getContext('2d', { alpha: true })
  context?.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0)

  if (sizeChanged) rebuildSurface()
  paintOcean()
}

/**
 * 以 30 FPS 上限驱动动画。
 * 为什么这么做：像素潮汐不需要高帧率，限制刷新可以减少登录页空闲功耗；
 * 好处：保持视觉连贯的同时，为低功耗设备和后台标签页节省资源。
 *
 * @param now 浏览器提供的当前高精度时间戳。
 */
function animate(now: number) {
  if (lastFrameAt === 0) lastFrameAt = now - FRAME_INTERVAL
  const deltaMs = now - lastFrameAt

  if (deltaMs >= FRAME_INTERVAL) {
    updateSimulation(now, Math.min(deltaMs, 50))
    paintOcean()
    lastFrameAt = now
  }

  animationFrameId = window.requestAnimationFrame(animate)
}

/**
 * 在允许动态效果且页面可见时启动潮汐。
 * 为什么这么做：重复启动会产生多条 requestAnimationFrame 链；
 * 好处：单一循环更易回收，也能避免动画速度意外叠加。
 */
function startAnimation() {
  if (animationFrameId || motionPreference?.matches || document.hidden) return

  const now = performance.now()
  lastFrameAt = 0
  if (nextTideAt <= now) scheduleNextTide(now)
  if (nextRippleAt <= now) nextRippleAt = now + 900
  animationFrameId = window.requestAnimationFrame(animate)
}

/**
 * 停止并清理当前动画帧。
 * 为什么这么做：路由切换或页面隐藏后继续绘制没有用户价值；
 * 好处：避免无效 CPU 占用和卸载后的 Canvas 访问。
 */
function stopAnimation() {
  if (animationFrameId) window.cancelAnimationFrame(animationFrameId)
  animationFrameId = 0
  lastFrameAt = 0
}

/**
 * 响应页面可见性变化。
 * 为什么这么做：浏览器后台标签页不需要持续模拟波浪；
 * 好处：返回页面时可安全续播，并显著降低后台资源使用。
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
 * 为什么这么做：持续涨潮可能让对运动敏感的用户不适；
 * 好处：开启减少动态后仅显示一帧低潮画面，仍保留品牌视觉但不再运动。
 */
function handleMotionPreferenceChange() {
  stopAnimation()
  surfaceOffsets.fill(0)
  surfaceVelocities.fill(0)

  if (motionPreference?.matches) {
    currentTideDepth = 0.32
    targetTideDepth = 0.32
    paintOcean()
    return
  }

  scheduleNextTide(performance.now())
  startAnimation()
}

/**
 * 初始化 Canvas、尺寸监听和动画偏好。
 * 为什么这么做：所有浏览器资源都从同一入口创建；
 * 好处：组件卸载时可以一一对应释放，避免登录页反复进入后累积监听器。
 */
function initializeOcean() {
  const canvas = canvasRef.value
  if (!canvas) return

  motionPreference = window.matchMedia('(prefers-reduced-motion: reduce)')
  motionPreference.addEventListener('change', handleMotionPreferenceChange)
  document.addEventListener('visibilitychange', handleVisibilityChange)

  resizeObserver = new ResizeObserver(resizeCanvas)
  resizeObserver.observe(canvas)
  resizeCanvas()
  startAnimation()
}

/**
 * 释放 Canvas 动画与浏览器监听器。
 * 为什么这么做：登录成功后组件会被路由卸载；
 * 好处：确保工作台不再承担登录背景的绘制和监听成本。
 */
function disposeOcean() {
  stopAnimation()
  resizeObserver?.disconnect()
  motionPreference?.removeEventListener('change', handleMotionPreferenceChange)
  document.removeEventListener('visibilitychange', handleVisibilityChange)
  resizeObserver = null
  motionPreference = null
  context = null
}

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
