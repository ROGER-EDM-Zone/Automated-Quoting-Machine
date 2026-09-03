import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import Queue from "./pages/Queue";
import EnquiryWorkspace from "./pages/EnquiryWorkspace";
import CustomerPage from "./pages/CustomerPage";
import AdminRates from "./pages/AdminRates";
import AdminRules from "./pages/AdminRules";
import Reports from "./pages/Reports";

export default function App() {
  return (
    <>
      <header className="app-header">
        <h1>Quoting workspace</h1>
        <nav>
          <NavLink to="/queue">Queue</NavLink>
          <NavLink to="/admin/rates">Rates</NavLink>
          <NavLink to="/admin/rules">Rules</NavLink>
          <NavLink to="/reports">Reports</NavLink>
        </nav>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Navigate to="/queue" replace />} />
          <Route path="/queue" element={<Queue />} />
          <Route path="/enquiry/:id" element={<EnquiryWorkspace />} />
          <Route path="/customer/:id" element={<CustomerPage />} />
          <Route path="/admin/rates" element={<AdminRates />} />
          <Route path="/admin/rules" element={<AdminRules />} />
          <Route path="/reports" element={<Reports />} />
        </Routes>
      </main>
    </>
  );
}
