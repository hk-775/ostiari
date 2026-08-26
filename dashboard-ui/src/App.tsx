import { Routes, Route } from 'react-router'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Traces from './pages/Traces'
import Breakers from './pages/Breakers'
import Agents from './pages/Agents'
import Reports from './pages/Reports'

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/traces" element={<Traces />} />
        <Route path="/breakers" element={<Breakers />} />
        <Route path="/agents" element={<Agents />} />
        <Route path="/reports" element={<Reports />} />
      </Routes>
    </Layout>
  )
}
