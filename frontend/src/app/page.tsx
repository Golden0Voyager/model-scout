"use client";

import { useEffect, useState, useMemo, useCallback, useRef } from "react";
import {
  Activity,
  Zap,
  RefreshCw,
  Search,
  Wifi,
  WifiOff,
  HelpCircle,
  Key,
  Clock,
  Database,
  Server,
  ChevronDown,
  ChevronRight,
  Filter,
  Copy,
  Check,
  LayoutGrid,
  List,
  ArrowUpDown,
  Gem,
  GripVertical,
} from "lucide-react";
import ModelModal from "./components/ModelModal";
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  verticalListSortingStrategy,
  useSortable,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";

interface Health {
  status: string;
  latency_ms?: number;
  error_message?: string;
  last_checked?: string;
}

interface Model {
  id: string;
  name: string;
  provider: string;
  provider_name: string;
  context_length: number;
  max_output_tokens?: number;
  description: string;
  description_cn: string;
  capabilities: string[];
  pricing_input_per_1m?: number;
  pricing_output_per_1m?: number;
  pricing_currency: string;
  pricing_note: string;
  is_free: boolean;
  health: Health;
}

interface ProviderSummary {
  key: string;
  name: string;
  model_count: number;
  online_count: number;
  avg_latency_ms?: number;
}

interface DashboardData {
  models: Model[];
  providers: ProviderSummary[];
  total_models: number;
  online_models: number;
  avg_latency_ms?: number;
  last_scan_time?: string;
  is_scanning: boolean;
}

type SortKey = "default" | "name" | "latency" | "price" | "context" | "status";
type ProviderSortKey = "default" | "name" | "online_rate" | "latency" | "manual";
type ViewMode = "card" | "list";
type TabFilter = "all" | "online" | "free" | "chat" | "vision" | "coding" | "reasoning" | "long_context";

const STATUS_META: Record<string, { label: string; color: string; icon: React.ReactNode; sortOrder: number }> = {
  online: { label: "Online", color: "#22c55e", icon: <Wifi className="w-3.5 h-3.5" />, sortOrder: 0 },
  offline: { label: "Offline", color: "#ef4444", icon: <WifiOff className="w-3.5 h-3.5" />, sortOrder: 1 },
  error: { label: "Error", color: "#f59e0b", icon: <Activity className="w-3.5 h-3.5" />, sortOrder: 2 },
  no_key: { label: "No Key", color: "#64748b", icon: <Key className="w-3.5 h-3.5" />, sortOrder: 3 },
  unknown: { label: "Unknown", color: "#3b82f6", icon: <HelpCircle className="w-3.5 h-3.5" />, sortOrder: 4 },
};

function latencyColor(ms: number): string {
  if (ms < 200) return "#22c55e";   // emerald-500 - excellent
  if (ms < 500) return "#f59e0b";   // amber-500   - good
  if (ms < 1000) return "#f97316";  // orange-500  - fair
  return "#ef4444";                  // red-500     - poor
}

const CNY_TO_USD = 7.2;

function formatPrice(input?: number, output?: number, currency?: string): string {
  if (input == null || output == null) return "Pricing N/A";
  const cny = currency === "CNY" || !currency;
  if (cny) {
    const inUsd = input / CNY_TO_USD;
    const outUsd = output / CNY_TO_USD;
    return `$${inUsd.toFixed(2)} / $${outUsd.toFixed(2)} per 1M tokens (¥${input.toFixed(2)} / ¥${output.toFixed(2)})`;
  }
  // USD or other currency — display as-is
  return `$${input.toFixed(2)} / $${output.toFixed(2)} per 1M tokens`;
}

const CAPABILITY_LABELS: Record<string, string> = {
  chat: "Chat",
  coding: "Coding",
  reasoning: "Reasoning",
  vision: "Vision",
  function_calling: "Tools",
  long_context: "Long Context",
  moE: "MoE",
};

const PROVIDER_COLORS: Record<string, string> = {
  scnet: "#0ea5e9",
  aihubmix: "#a855f7",
  openrouter: "#6366f1",
  groq: "#f97316",
  dashscope: "#e11d48",
  siliconflow: "#0284c7",
  zhipuai: "#f59e0b",
  deepseek: "#10b981",
  moonshot: "#ec4899",
  anyrouter: "#8b5cf6",
  agentrouter: "#06b6d4",
};

const TAB_CONFIG: { key: TabFilter; label: string; icon: React.ReactNode }[] = [
  { key: "all", label: "All", icon: <LayoutGrid className="w-3.5 h-3.5" /> },
  { key: "online", label: "Online", icon: <Wifi className="w-3.5 h-3.5" /> },
  { key: "free", label: "Free", icon: <Gem className="w-3.5 h-3.5" /> },
  { key: "chat", label: "Chat", icon: null },
  { key: "vision", label: "Vision", icon: null },
  { key: "coding", label: "Coding", icon: null },
  { key: "reasoning", label: "Reasoning", icon: null },
  { key: "long_context", label: "Long Context", icon: null },
];

const SORT_OPTIONS: { key: SortKey; label: string }[] = [
  { key: "default", label: "Default" },
  { key: "status", label: "Online First" },
  { key: "name", label: "Name" },
  { key: "latency", label: "Latency (Low→High)" },
  { key: "price", label: "Price (Low→High)" },
  { key: "context", label: "Context (High→Low)" },
];

const PROVIDER_SORT_OPTIONS: { key: ProviderSortKey; label: string }[] = [
  { key: "default", label: "Default" },
  { key: "online_rate", label: "Online Rate (High→Low)" },
  { key: "latency", label: "Latency (Low→High)" },
  { key: "name", label: "Name (A→Z)" },
  { key: "manual", label: "Manual (Drag)" },
];

export default function Home() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [isScanning, setIsScanning] = useState(false);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [expandedProviders, setExpandedProviders] = useState<Set<string>>(new Set());
  const [capFilter, setCapFilter] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>("default");
  const [providerSortKey, setProviderSortKey] = useState<ProviderSortKey>("manual");
  const [viewMode, setViewMode] = useState<ViewMode>("card");
  const [tabFilter, setTabFilter] = useState<TabFilter>("all");
  const [sortOpen, setSortOpen] = useState(false);
  const [providerSortOpen, setProviderSortOpen] = useState(false);
  const [scanningProviders, setScanningProviders] = useState<Set<string>>(new Set());
  const [scanningModels, setScanningModels] = useState<Set<string>>(new Set());
  const [selectedModel, setSelectedModel] = useState<Model | null>(null);
  const [providerOrder, setProviderOrder] = useState<string[]>(() => {
    if (typeof window !== "undefined") {
      try {
        const saved = localStorage.getItem("modelscout_provider_order");
        if (saved) return JSON.parse(saved);
      } catch {}
    }
    return [];
  });
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates })
  );
  const hasInit = useRef(false);
  const sortRef = useRef<HTMLDivElement>(null);
  const providerSortRef = useRef<HTMLDivElement>(null);

  // Close sort dropdowns on outside click
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (sortRef.current && !sortRef.current.contains(e.target as Node)) {
        setSortOpen(false);
      }
      if (providerSortRef.current && !providerSortRef.current.contains(e.target as Node)) {
        setProviderSortOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch("/api/models");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json: DashboardData = await res.json();
      setData(json);
      setIsScanning(json.is_scanning);
      setError(null);

      if (!hasInit.current && json.models.length > 0) {
        setExpandedProviders(new Set());
        hasInit.current = true;
      }
    } catch (e) {
      setError("Backend connection failed");
    }
  }, []);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 30000);
    const onVis = () => {
      if (!document.hidden) fetchData();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [fetchData]);

  const triggerScan = async () => {
    try {
      setIsScanning(true);
      await fetch("/api/scan", { method: "POST" });
      setTimeout(fetchData, 800);
    } catch {
      setError("Failed to trigger scan");
    }
  };

  const triggerProviderScan = async (providerKey: string) => {
    try {
      setScanningProviders((prev) => new Set(prev).add(providerKey));
      await fetch(`/api/scan/${providerKey}`, { method: "POST" });
      setTimeout(() => {
        fetchData();
        setScanningProviders((prev) => {
          const n = new Set(prev);
          n.delete(providerKey);
          return n;
        });
      }, 1200);
    } catch {
      setError("Failed to trigger provider scan");
      setScanningProviders((prev) => {
        const n = new Set(prev);
        n.delete(providerKey);
        return n;
      });
    }
  };

  const triggerModelScan = async (providerKey: string, modelId: string) => {
    const key = `${providerKey}:${modelId}`;
    try {
      setScanningModels((prev) => new Set(prev).add(key));
      await fetch(`/api/scan/${providerKey}/${encodeURIComponent(modelId)}`, { method: "POST" });
      setTimeout(() => {
        fetchData();
        setScanningModels((prev) => {
          const n = new Set(prev);
          n.delete(key);
          return n;
        });
      }, 800);
    } catch {
      setError("Failed to trigger model scan");
      setScanningModels((prev) => {
        const n = new Set(prev);
        n.delete(key);
        return n;
      });
    }
  };

  const filteredModels = useMemo(() => {
    if (!data) return [];
    let ms = data.models;

    // Tab filter
    if (tabFilter === "online") {
      ms = ms.filter((m) => m.health.status === "online");
    } else if (tabFilter === "free") {
      ms = ms.filter((m) => m.is_free);
    } else if (tabFilter !== "all") {
      ms = ms.filter((m) => m.capabilities.includes(tabFilter));
    }

    // Search filter
    const q = filter.trim().toLowerCase();
    if (q) {
      ms = ms.filter(
        (m) =>
          m.name.toLowerCase().includes(q) ||
          m.id.toLowerCase().includes(q) ||
          m.provider_name.toLowerCase().includes(q) ||
          m.description_cn.includes(q) ||
          m.description.toLowerCase().includes(q)
      );
    }

    // Capability filter
    if (capFilter) {
      ms = ms.filter((m) => m.capabilities.includes(capFilter));
    }

    // Sort
    ms = [...ms].sort((a, b) => {
      switch (sortKey) {
        case "name":
          return a.name.localeCompare(b.name);
        case "latency": {
          const la = a.health.latency_ms ?? 99999;
          const lb = b.health.latency_ms ?? 99999;
          return la - lb;
        }
        case "price": {
          const pa = a.pricing_input_per_1m ?? (a.is_free ? 0 : 99999);
          const pb = b.pricing_input_per_1m ?? (b.is_free ? 0 : 99999);
          return pa - pb;
        }
        case "context":
          return b.context_length - a.context_length;
        case "status": {
          const sa = STATUS_META[a.health.status]?.sortOrder ?? 99;
          const sb = STATUS_META[b.health.status]?.sortOrder ?? 99;
          if (sa !== sb) return sa - sb;
          const la = a.health.latency_ms ?? 99999;
          const lb = b.health.latency_ms ?? 99999;
          return la - lb;
        }
        default:
          // Default: provider order, then online first, then latency
          const pIdx = data.providers.findIndex((p) => p.key === a.provider);
          const pIdxB = data.providers.findIndex((p) => p.key === b.provider);
          if (pIdx !== pIdxB) return pIdx - pIdxB;
          const sa = a.health.status === "online" ? 0 : 1;
          const sb = b.health.status === "online" ? 0 : 1;
          if (sa !== sb) return sa - sb;
          const la = a.health.latency_ms ?? 99999;
          const lb = b.health.latency_ms ?? 99999;
          return la - lb;
      }
    });

    return ms;
  }, [data, filter, capFilter, sortKey, tabFilter]);

  const grouped = useMemo(() => {
    const g: Record<string, Model[]> = {};
    for (const m of filteredModels) {
      if (!g[m.provider]) g[m.provider] = [];
      g[m.provider].push(m);
    }
    return g;
  }, [filteredModels]);

  // Sort providers
  const sortedProviders = useMemo(() => {
    if (!data) return [];
    const providers = [...data.providers];

    if (providerSortKey === "manual" && providerOrder.length > 0) {
      const orderMap = new Map(providerOrder.map((k, i) => [k, i]));
      providers.sort((a, b) => {
        const ia = orderMap.get(a.key) ?? 9999;
        const ib = orderMap.get(b.key) ?? 9999;
        return ia - ib;
      });
      return providers;
    }

    providers.sort((a, b) => {
      switch (providerSortKey) {
        case "name":
          return a.name.localeCompare(b.name);
        case "online_rate": {
          const ra = a.online_count / Math.max(a.model_count, 1);
          const rb = b.online_count / Math.max(b.model_count, 1);
          if (rb !== ra) return rb - ra;
          return a.name.localeCompare(b.name);
        }
        case "latency": {
          const la = a.avg_latency_ms ?? 99999;
          const lb = b.avg_latency_ms ?? 99999;
          return la - lb;
        }
        default:
          return 0;
      }
    });
    return providers;
  }, [data, providerSortKey, providerOrder]);

  const allCapabilities = useMemo(() => {
    if (!data) return [];
    const s = new Set<string>();
    data.models.forEach((m) => m.capabilities.forEach((c) => s.add(c)));
    return Array.from(s);
  }, [data]);

  const tabCounts = useMemo(() => {
    if (!data) return {} as Record<TabFilter, number>;
    const counts = {} as Record<TabFilter, number>;
    counts.all = data.models.length;
    counts.online = data.models.filter((m) => m.health.status === "online").length;
    counts.free = data.models.filter((m) => m.is_free).length;
    counts.chat = data.models.filter((m) => m.capabilities.includes("chat")).length;
    counts.vision = data.models.filter((m) => m.capabilities.includes("vision")).length;
    counts.coding = data.models.filter((m) => m.capabilities.includes("coding")).length;
    counts.reasoning = data.models.filter((m) => m.capabilities.includes("reasoning")).length;
    counts.long_context = data.models.filter((m) => m.capabilities.includes("long_context")).length;
    return counts;
  }, [data]);

  const handleProviderDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (over && active.id !== over.id) {
      setProviderOrder((prev) => {
        const oldIndex = prev.indexOf(active.id as string);
        const newIndex = prev.indexOf(over.id as string);
        let next: string[];
        if (oldIndex === -1 || newIndex === -1) {
          // Build full list from current sorted providers
          const currentKeys = sortedProviders.map((p) => p.key);
          const fromIndex = currentKeys.indexOf(active.id as string);
          const toIndex = currentKeys.indexOf(over.id as string);
          next = arrayMove(currentKeys, fromIndex, toIndex);
        } else {
          next = arrayMove(prev, oldIndex, newIndex);
        }
        localStorage.setItem("modelscout_provider_order", JSON.stringify(next));
        return next;
      });
    }
  };

  const toggleProvider = (key: string) => {
    setExpandedProviders((prev) => {
      const n = new Set(prev);
      if (n.has(key)) n.delete(key);
      else n.add(key);
      return n;
    });
  };

  if (!data && !error) {
    return (
      <main className="min-h-screen bg-[#0f1117] flex items-center justify-center">
        <div className="flex items-center gap-3 text-slate-500">
          <RefreshCw className="w-5 h-5 animate-spin" />
          <span className="text-sm font-medium">Loading...</span>
        </div>
      </main>
    );
  }

  return (
    <main className="min-h-screen bg-[#0f1117] text-slate-200">
      <div className="max-w-[1280px] mx-auto px-4 sm:px-6 py-8">
        {/* Header */}
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-6">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-indigo-500/10 border border-indigo-500/20 flex items-center justify-center">
              <Server className="w-5 h-5 text-indigo-400" />
            </div>
            <div>
              <h1 className="text-2xl font-bold text-white tracking-tight">ModelScout</h1>
              <p className="text-xs text-slate-500">Model Availability Monitor</p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-500" />
              <input
                type="text"
                placeholder="Search models / providers..."
                className="bg-slate-900 border border-slate-800 rounded-lg py-2 pl-9 pr-4 text-sm w-56 focus:outline-none focus:border-indigo-500/50 placeholder:text-slate-600"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              />
            </div>
            <button
              onClick={triggerScan}
              disabled={isScanning}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all ${
                isScanning
                  ? "bg-slate-800 text-slate-500 cursor-not-allowed"
                  : "bg-indigo-600 hover:bg-indigo-500 text-white"
              }`}
            >
              {isScanning ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Zap className="w-4 h-4" />}
              {isScanning ? "Scanning" : "Sync"}
            </button>
          </div>
        </div>

        {/* Stats */}
        {data && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-5">
            <StatCard label="Total" value={`${data.total_models}`} icon={<Database className="w-4 h-4 text-indigo-400" />} />
            <StatCard label="Online" value={`${data.online_models}`} icon={<Wifi className="w-4 h-4 text-emerald-400" />} />
            <StatCard label="Avg Latency" value={data.avg_latency_ms ? `${data.avg_latency_ms}ms` : "--"} icon={<Clock className="w-4 h-4 text-amber-400" />} />
            <StatCard
              label="Last Sync"
              value={data.last_scan_time ? formatTime(data.last_scan_time) : "--"}
              icon={<Activity className="w-4 h-4 text-cyan-400" />}
            />
          </div>
        )}

        {/* Tab filters + Sort + View toggle */}
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-5">
          <div className="flex items-center gap-1 overflow-x-auto pb-1 scrollbar-hide">
            {TAB_CONFIG.map((tab) => {
              const count = tabCounts[tab.key] ?? 0;
              const active = tabFilter === tab.key;
              return (
                <button
                  key={tab.key}
                  onClick={() => setTabFilter(tab.key)}
                  className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all whitespace-nowrap ${
                    active
                      ? "bg-indigo-500/15 text-indigo-300 border border-indigo-500/20"
                      : "text-slate-500 hover:text-slate-300 border border-transparent"
                  }`}
                >
                  {tab.icon}
                  {tab.label}
                  <span className={`text-[10px] px-1 py-0.5 rounded ${active ? "bg-indigo-500/20 text-indigo-300" : "bg-slate-800 text-slate-500"}`}>
                    {count}
                  </span>
                </button>
              );
            })}
          </div>

          <div className="flex items-center gap-2">
            {/* Sort dropdown */}
            <div className="relative" ref={sortRef}>
              <button
                onClick={() => setSortOpen(!sortOpen)}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium text-slate-400 hover:text-slate-200 bg-slate-900 border border-slate-800 hover:border-slate-700 transition-all"
              >
                <ArrowUpDown className="w-3.5 h-3.5" />
                {SORT_OPTIONS.find((o) => o.key === sortKey)?.label}
                <ChevronDown className={`w-3 h-3 transition-transform ${sortOpen ? "rotate-180" : ""}`} />
              </button>
              {sortOpen && (
                <div className="absolute right-0 top-full mt-1 bg-slate-900 border border-slate-800 rounded-lg shadow-xl py-1 z-50 min-w-[160px]">
                  {SORT_OPTIONS.map((o) => (
                    <button
                      key={o.key}
                      onClick={() => {
                        setSortKey(o.key);
                        setSortOpen(false);
                      }}
                      className={`w-full text-left px-3 py-1.5 text-xs transition-colors ${
                        sortKey === o.key ? "text-indigo-300 bg-indigo-500/10" : "text-slate-400 hover:text-slate-200 hover:bg-slate-800"
                      }`}
                    >
                      {o.label}
                    </button>
                  ))}
                </div>
              )}
            </div>

            {/* View toggle */}
            <div className="flex items-center bg-slate-900 border border-slate-800 rounded-lg p-0.5">
              <button
                onClick={() => setViewMode("card")}
                className={`p-1.5 rounded-md transition-colors ${viewMode === "card" ? "bg-slate-700 text-white" : "text-slate-500 hover:text-slate-300"}`}
                title="Card View"
              >
                <LayoutGrid className="w-3.5 h-3.5" />
              </button>
              <button
                onClick={() => setViewMode("list")}
                className={`p-1.5 rounded-md transition-colors ${viewMode === "list" ? "bg-slate-700 text-white" : "text-slate-500 hover:text-slate-300"}`}
                title="List View"
              >
                <List className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
        </div>

        {/* Capability filter */}
        {allCapabilities.length > 0 && (
          <div className="flex items-center gap-2 mb-5 overflow-x-auto pb-1">
            <Filter className="w-3.5 h-3.5 text-slate-500 shrink-0" />
            <button
              onClick={() => setCapFilter(null)}
              className={`px-2.5 py-1 rounded-md text-xs font-medium transition-colors ${
                !capFilter ? "bg-indigo-500/15 text-indigo-300" : "text-slate-500 hover:text-slate-300"
              }`}
            >
              All Capabilities
            </button>
            {allCapabilities.map((c) => (
              <button
                key={c}
                onClick={() => setCapFilter(c === capFilter ? null : c)}
                className={`px-2.5 py-1 rounded-md text-xs font-medium transition-colors whitespace-nowrap ${
                  c === capFilter ? "bg-indigo-500/15 text-indigo-300" : "text-slate-500 hover:text-slate-300"
                }`}
              >
                {CAPABILITY_LABELS[c] || c}
              </button>
            ))}
          </div>
        )}

        {error && (
          <div className="mb-6 bg-red-500/10 border border-red-500/20 p-3 rounded-lg text-red-400 text-sm">
            {error}
          </div>
        )}

        {/* Results count */}
        {data && (
          <div className="mb-4 text-xs text-slate-500">
            Showing {filteredModels.length} / {data.total_models} models
          </div>
        )}

        {/* Provider sort toolbar */}
        {data && (
          <div className="flex items-center justify-between mb-4">
            <div className="text-xs text-slate-500">
              {sortedProviders.length} providers
            </div>
            <div className="relative" ref={providerSortRef}>
              <button
                onClick={() => setProviderSortOpen(!providerSortOpen)}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium text-slate-400 hover:text-slate-200 bg-slate-900 border border-slate-800 hover:border-slate-700 transition-all"
              >
                <ArrowUpDown className="w-3.5 h-3.5" />
                <span>Provider</span>
                {PROVIDER_SORT_OPTIONS.find((o) => o.key === providerSortKey)?.label}
                <ChevronDown className={`w-3 h-3 transition-transform ${providerSortOpen ? "rotate-180" : ""}`} />
              </button>
              {providerSortOpen && (
                <div className="absolute right-0 top-full mt-1 bg-slate-900 border border-slate-800 rounded-lg shadow-xl py-1 z-50 min-w-[180px]">
                  {PROVIDER_SORT_OPTIONS.map((o) => (
                    <button
                      key={o.key}
                      onClick={() => {
                        setProviderSortKey(o.key);
                        setProviderSortOpen(false);
                      }}
                      className={`w-full text-left px-3 py-1.5 text-xs transition-colors ${
                        providerSortKey === o.key ? "text-indigo-300 bg-indigo-500/10" : "text-slate-400 hover:text-slate-200 hover:bg-slate-800"
                      }`}
                    >
                      {o.label}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        {/* Content */}
        <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleProviderDragEnd}>
          <SortableContext items={sortedProviders.map((p) => p.key)} strategy={verticalListSortingStrategy}>
            {viewMode === "card" ? (
              /* Card View */
              <div className="space-y-6">
            {sortedProviders.map((provider) => {
              const models = grouped[provider.key] || [];
              if (models.length === 0) return null;
              return (
                <ProviderGroup
                  key={provider.key}
                  provider={provider}
                  sortKey={providerSortKey}
                  isExpanded={expandedProviders.has(provider.key)}
                  onToggle={() => toggleProvider(provider.key)}
                  isScanning={scanningProviders.has(provider.key)}
                  onRefresh={() => triggerProviderScan(provider.key)}
                >
                  {expandedProviders.has(provider.key) && (
                    <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3 mt-4">
                      {models.map((m) => (
                        <ModelCard
                          key={`${m.provider}-${m.id}`}
                          model={m}
                          isScanning={scanningModels.has(`${m.provider}:${m.id}`)}
                          onRefresh={() => triggerModelScan(m.provider, m.id)}
                          onSelect={() => setSelectedModel(m)}
                        />
                      ))}
                    </div>
                  )}
                </ProviderGroup>
              );
            })}
          </div>
        ) : (
          /* List View */
          <div className="space-y-6">
            {sortedProviders.map((provider) => {
              const models = grouped[provider.key] || [];
              if (models.length === 0) return null;
              return (
                <ProviderGroup
                  key={provider.key}
                  provider={provider}
                  sortKey={providerSortKey}
                  isExpanded={expandedProviders.has(provider.key)}
                  onToggle={() => toggleProvider(provider.key)}
                  isScanning={scanningProviders.has(provider.key)}
                  onRefresh={() => triggerProviderScan(provider.key)}
                >
                  {expandedProviders.has(provider.key) && (
                    <div className="mt-2 border border-slate-800 rounded-xl overflow-hidden">
                      <div className="grid grid-cols-[1fr_80px_80px_100px_60px] gap-3 px-4 py-2 bg-slate-900/80 text-[10px] text-slate-500 font-medium uppercase tracking-wider">
                        <span>Model</span>
                        <span>Latency</span>
                        <span>Context</span>
                        <span>Price</span>
                        <span>Status</span>
                      </div>
                      {models.map((m) => (
                        <ModelListRow
                          key={`${m.provider}-${m.id}`}
                          model={m}
                          isScanning={scanningModels.has(`${m.provider}:${m.id}`)}
                          onRefresh={() => triggerModelScan(m.provider, m.id)}
                          onSelect={() => setSelectedModel(m)}
                        />
                      ))}
                    </div>
                  )}
                </ProviderGroup>
              );
            })}
          </div>
            )}
          </SortableContext>
        </DndContext>

        {filteredModels.length === 0 && data && (
          <div className="py-20 text-center text-slate-600">
            <Search className="w-10 h-10 mx-auto mb-3 opacity-30" />
            <p className="text-sm">No matching models found</p>
          </div>
        )}
      </div>

      <ModelModal
        model={selectedModel}
        onClose={() => setSelectedModel(null)}
        onRefresh={
          selectedModel
            ? () => triggerModelScan(selectedModel.provider, selectedModel.id)
            : undefined
        }
        isScanning={
          selectedModel
            ? scanningModels.has(`${selectedModel.provider}:${selectedModel.id}`)
            : false
        }
      />
    </main>
  );
}

function ProviderGroup({
  provider,
  sortKey,
  isExpanded,
  onToggle,
  isScanning,
  onRefresh,
  children,
}: {
  provider: any;
  sortKey: string;
  isExpanded: boolean;
  onToggle: () => void;
  isScanning?: boolean;
  onRefresh?: () => void;
  children: React.ReactNode;
}) {
  const isManual = sortKey === "manual";
  const sortable = useSortable({ id: provider.key, disabled: !isManual });
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = isManual
    ? sortable
    : ({} as any);
  const style = isManual ? { transform: CSS.Transform.toString(transform), transition } : {};
  const color = PROVIDER_COLORS[provider.key] || "#6366f1";

  return (
    <section
      ref={isManual ? setNodeRef : undefined}
      style={style}
      className={isManual && isDragging ? "opacity-50" : undefined}
    >
      <div className="w-full flex items-center justify-between py-3 border-b border-slate-800 hover:border-slate-700 transition-colors group">
        <button onClick={onToggle} className="flex items-center gap-3 flex-1 text-left">
          {isManual && (
            <div
              {...attributes}
              {...listeners}
              className="cursor-grab active:cursor-grabbing p-1 -ml-2 rounded hover:bg-slate-800"
            >
              <GripVertical className="w-4 h-4 text-slate-500" />
            </div>
          )}
          <div className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: color }} />
          <h2 className="text-base font-semibold text-white">{provider.name}</h2>
          <span className="text-xs text-slate-500">
            {provider.online_count}/{provider.model_count} Online
          </span>
          {provider.avg_latency_ms && (
            <span className="text-xs text-slate-600">avg {provider.avg_latency_ms}ms</span>
          )}
        </button>
        <div className="flex items-center gap-2">
          {onRefresh && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                onRefresh();
              }}
              disabled={isScanning}
              className={`p-1.5 rounded-md transition-all ${
                isScanning
                  ? "text-indigo-400 animate-spin"
                  : "text-slate-600 hover:text-indigo-400 hover:bg-indigo-500/10 opacity-0 group-hover:opacity-100"
              }`}
              title="Refresh provider"
            >
              <RefreshCw className="w-3.5 h-3.5" />
            </button>
          )}
          <button onClick={onToggle} className="text-slate-600 group-hover:text-slate-400 transition-colors">
            {isExpanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
          </button>
        </div>
      </div>
      {children}
    </section>
  );
}

function ModelCard({ model, isScanning, onRefresh, onSelect }: { model: Model; isScanning?: boolean; onRefresh?: () => void; onSelect?: () => void }) {
  const h = model.health;
  const meta = STATUS_META[h.status] || STATUS_META.unknown;
  const [copied, setCopied] = useState(false);

  const copyId = () => {
    navigator.clipboard.writeText(model.id);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div
      className="model-card bg-slate-900/50 border border-slate-800 rounded-xl p-4 hover:border-slate-700 transition-colors group relative cursor-pointer"
      onClick={() => onSelect?.()}
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold text-white truncate">{model.name}</h3>
            {model.is_free && (
              <span className="text-[9px] px-1 py-0.5 rounded bg-emerald-500/10 text-emerald-400 font-medium shrink-0">
                Free
              </span>
            )}
          </div>
          <div className="flex items-center gap-1.5 mt-0.5">
            <p className="text-[11px] text-slate-500 font-mono truncate">{model.id}</p>
            <button
              onClick={copyId}
              className="text-slate-600 hover:text-slate-300 transition-colors opacity-0 group-hover:opacity-100"
              title="Copy Model ID"
            >
              {copied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
            </button>
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          {onRefresh && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                onRefresh();
              }}
              disabled={isScanning}
              className={`p-1 rounded transition-all ${
                isScanning
                  ? "text-indigo-400 animate-spin"
                  : "text-slate-600 hover:text-indigo-400 hover:bg-indigo-500/10 opacity-0 group-hover:opacity-100"
              }`}
              title="Refresh model"
            >
              <RefreshCw className="w-3 h-3" />
            </button>
          )}
          <div
            className="shrink-0 flex items-center gap-1.5 px-2 py-1 rounded-md border"
            style={{
              borderColor: `${meta.color}20`,
              backgroundColor: `${meta.color}10`,
            }}
          >
            <div
              className={`w-1.5 h-1.5 rounded-full ${h.status === "online" ? "status-pulse" : ""}`}
              style={{ backgroundColor: meta.color }}
            />
            <span className="text-[10px] font-medium" style={{ color: meta.color }}>
              {meta.label}
            </span>
            {h.latency_ms && (
              <span className="text-[10px] ml-0.5 font-medium" style={{ color: latencyColor(h.latency_ms) }}>
                {h.latency_ms}ms
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Metrics row */}
      <div className="flex items-center gap-4 mb-3 text-xs">
        <div className="flex items-center gap-1 text-slate-400">
          <Database className="w-3 h-3" />
          <span>{formatContext(model.context_length)}</span>
        </div>
        {model.max_output_tokens && (
          <div className="flex items-center gap-1 text-slate-400">
            <Zap className="w-3 h-3" />
            <span>Out {formatContext(model.max_output_tokens)}</span>
          </div>
        )}
      </div>

      {/* Capabilities */}
      {model.capabilities.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-3">
          {model.capabilities.map((c) => (
            <span
              key={c}
              className="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 font-medium"
            >
              {CAPABILITY_LABELS[c] || c}
            </span>
          ))}
        </div>
      )}

      {/* Pricing */}
      <div className="mb-3">
        {model.is_free ? (
          <span className="text-[11px] font-medium text-emerald-400">
            Free
          </span>
        ) : (
          <span className="text-[11px] text-slate-400">
            {formatPrice(model.pricing_input_per_1m, model.pricing_output_per_1m, model.pricing_currency)}
            {model.pricing_note && <span className="text-slate-600 ml-1">· {model.pricing_note}</span>}
          </span>
        )}
      </div>

      {/* Description */}
      <p className="text-[11px] text-slate-500 leading-relaxed line-clamp-2">
        {model.description_cn || model.description}
      </p>

      {/* Error */}
      {h.error_message && h.status !== "online" && (
        <p className="mt-2 text-[10px] text-red-400/80 line-clamp-1">{h.error_message}</p>
      )}
    </div>
  );
}

function ModelListRow({ model, isScanning, onRefresh, onSelect }: { model: Model; isScanning?: boolean; onRefresh?: () => void; onSelect?: () => void }) {
  const h = model.health;
  const meta = STATUS_META[h.status] || STATUS_META.unknown;
  const [copied, setCopied] = useState(false);

  const copyId = () => {
    navigator.clipboard.writeText(model.id);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div
      className="grid grid-cols-[1fr_80px_80px_100px_60px] gap-3 px-4 py-2.5 border-t border-slate-800/50 hover:bg-slate-800/30 transition-colors items-center group relative cursor-pointer"
      onClick={() => onSelect?.()}
    >
      {/* Name + ID */}
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-white truncate">{model.name}</span>
          {model.is_free && (
            <span className="text-[9px] px-1 py-0.5 rounded bg-emerald-500/10 text-emerald-400 font-medium shrink-0">
              Free
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5 mt-0.5">
          <span className="text-[10px] text-slate-500 font-mono truncate">{model.id}</span>
          <button
            onClick={copyId}
            className="text-slate-600 hover:text-slate-300 transition-colors opacity-0 group-hover:opacity-100"
            title="Copy Model ID"
          >
            {copied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
          </button>
          <span className="text-[10px] text-slate-600">· {model.provider_name}</span>
        </div>
      </div>

      {/* Latency */}
      <div className="flex items-center gap-1.5">
        <div className="text-xs tabular-nums font-medium" style={{ color: h.latency_ms ? latencyColor(h.latency_ms) : "#94a3b8" }}>
          {h.latency_ms ? `${h.latency_ms}ms` : "--"}
        </div>
        {onRefresh && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              onRefresh();
            }}
            disabled={isScanning}
            className={`p-0.5 rounded transition-all ${
              isScanning
                ? "text-indigo-400 animate-spin"
                : "text-slate-600 hover:text-indigo-400 hover:bg-indigo-500/10 opacity-0 group-hover:opacity-100"
            }`}
            title="Refresh model"
          >
            <RefreshCw className="w-3 h-3" />
          </button>
        )}
      </div>

      {/* Context */}
      <div className="text-xs text-slate-400">
        {formatContext(model.context_length)}
      </div>

      {/* Pricing */}
      <div className="text-[10px]">
        {model.is_free ? (
          <span className="text-emerald-400 font-medium">Free</span>
        ) : (
          <span className="text-slate-400">
            {formatPrice(model.pricing_input_per_1m, model.pricing_output_per_1m, model.pricing_currency)}
          </span>
        )}
      </div>

      {/* Status */}
      <div className="flex items-center gap-1.5">
        <div
          className={`w-1.5 h-1.5 rounded-full ${h.status === "online" ? "status-pulse" : ""}`}
          style={{ backgroundColor: meta.color }}
        />
        <span className="text-[10px] font-medium" style={{ color: meta.color }}>
          {meta.label}
        </span>
        {h.error_message && h.status !== "online" && (
          <span className="text-[9px] text-amber-500/80 truncate max-w-[80px]" title={h.error_message}>
            {h.error_message}
          </span>
        )}
      </div>
    </div>
  );
}

function StatCard({ label, value, icon }: { label: string; value: string; icon: React.ReactNode }) {
  return (
    <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4">
      <div className="flex items-center gap-2 mb-2">
        {icon}
        <span className="text-xs text-slate-500 font-medium">{label}</span>
      </div>
      <div className="text-xl font-bold text-white tabular-nums">{value}</div>
    </div>
  );
}

function formatContext(n: number): string {
  if (n >= 1000000) return `${(n / 1000000).toFixed(0)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(0)}K`;
  return `${n}`;
}

function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}
