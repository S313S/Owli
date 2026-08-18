import { Button, Modal, Space } from 'antd'
import type { ServerAction } from './types'

export default function ActionButtons({ actions }: { actions: ServerAction[] }) {
  async function execute(action: ServerAction) {
    const response = await fetch(action.href, { method: action.method })
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
