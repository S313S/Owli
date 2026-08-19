import { Button, Modal, Space } from 'antd'
import type { ServerAction } from './types'

export default function ActionButtons({ actions }: { actions: ServerAction[] }) {
  async function execute(action: ServerAction) {
    if (!action.href) throw new Error('后端没有提供操作地址')
    const response = await fetch(action.href, {
      method: action.method,
      headers: action.method === 'POST' ? { 'X-Request-ID': `action-${crypto.randomUUID()}` } : undefined,
    })
    if (!response.ok) throw new Error('操作没有成功，请稍后重试')
  }

  function invoke(action: ServerAction) {
    if (!action.confirm) {
      void execute(action)
      return
    }
    Modal.confirm({
      title: action.label,
      content: action.confirm,
      okText: action.label,
      okButtonProps: { danger: action.danger },
      cancelText: '取消',
      onOk: () => execute(action),
    })
  }

  return <Space>
    {actions.map((action) => <Button key={action.id} danger={action.danger} onClick={() => invoke(action)}>{action.label}</Button>)}
  </Space>
}
