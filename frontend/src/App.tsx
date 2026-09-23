import { Routes, Route } from "react-router-dom";
import Home from "@/pages/Home";
import Chart from "@/pages/Chart";

// One <Route> per page in src/pages; BrowserRouter already wraps this in main.tsx.
export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Home />} />
      <Route path="/chart" element={<Chart />} />
    </Routes>
  );
}
