import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import App from './App'
import './styles.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={{ token: {
      colorPrimary: '#1677ff', colorSuccess: '#52c41a', colorWarning: '#faad14',
      colorError: '#ff4d4f', borderRadius: 6, fontSize: 14,
    } }}>
      <App />
    </ConfigProvider>
  </React.StrictMode>,
)
