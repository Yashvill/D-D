import { NavLink, Route, Routes } from "react-router-dom";
import { HealthBanner } from "./components/HealthBanner";
import { FleetDashboard } from "./pages/FleetDashboard";
import { ContainerDetail } from "./pages/ContainerDetail";
import { ApprovalInbox } from "./pages/ApprovalInbox";

export default function App() {
  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="dot" />
          Persistent Fleet
        </div>
        <nav className="topnav">
          <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>
            Fleet
          </NavLink>
          <NavLink to="/approvals" className={({ isActive }) => (isActive ? "active" : "")}>
            Approvals
          </NavLink>
        </nav>
        <HealthBanner />
      </header>
      <main className="content">
        <Routes>
          <Route path="/" element={<FleetDashboard />} />
          <Route path="/container/:cid" element={<ContainerDetail />} />
          <Route path="/approvals" element={<ApprovalInbox />} />
        </Routes>
      </main>
    </div>
  );
}
