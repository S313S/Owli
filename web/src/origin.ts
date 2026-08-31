// FE-1：后端地址不写死。前端与后端同源（所有 /api 调用都是相对路径），
// 展示用的「本地服务地址」照 window.location.host 取真实来源，
// 任意端口 / 部署下都不会再显示成 127.0.0.1:8721 误导人。
export function backendOrigin(): string {
  if (typeof window === 'undefined') return '本地服务'
  return window.location.host || '本地服务'
}
