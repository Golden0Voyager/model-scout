"use client";

import { useEffect, useState, useMemo, useCallback, useRef } from "react";
import type { ReactNode } from "react";
import { 
  Activity,
  Zap, 
  ShieldCheck, 
  Database, 
  RefreshCcw, 
  Layers, 
  Cpu,
  Search,
  Wifi,
  BarChart3,
  Clock,
  ArrowUpRight,
  Monitor,
  ChevronDown,
  ChevronRight,
  AlertCircle
} from "lucide-react";

interface Model {
  id: string;
  name: string;
  provider: string;
  context_length: number;
  max_output_tokens?: number;
  description: string;
  description_cn?: string;
  pricing: string;
  performance?: {
    status: string;
    ttft?: number;
    tps?: number;
    last_checked?: string;
  };
}

const PROVIDER_THEMES: Record<string, string> = {
  "Groq": "rgb(249, 115, 22)",
  "OpenRouter": "rgb(99, 102, 241)",
  "DashScope": "rgb(225, 29, 72)",
  "SiliconFlow": "rgb(2, 132, 199)",
  "ZhipuAI": "rgb(245, 158, 11)",
  "AIHubMix": "rgb(168, 85, 247)",
  "SCNET": "rgb(20, 184, 166)"
};

export default function Home() {
  const [models, setModels] = useState<Model[]>([]);
  const [isScanning, setIsScanning] = useState(false);
  const [lastScan, setLastScan] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [expandedProviders, setExpandedProviders] = useState<Set<string>>(new Set());
  const hasInitializedProviders = useRef(false);
  const [sortBy, setSortBy] = useState<"name" | "latency" | "context">("name");

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch("/api/models");
      const data = await res.json();
      setModels(data.models || []);
      setIsScanning(data.is_scanning);
      setLastScan(data.last_scan_time);
      
      // Auto-expand all ONLY on the first successful load
      if (!hasInitializedProviders.current && data.models?.length > 0) {
        const providers = new Set(data.models.map((m: Model) => m.provider));
        setExpandedProviders(providers as Set<string>);
        hasInitializedProviders.current = true;
      }
    } catch (error) {
      console.error("Fetch error:", error);
      setError("无法连接到后端服务 API Connection Lost");
    }
  }, []);

  useEffect(() => {
    fetchData();
    let timer = setInterval(fetchData, 5000);

    const handleVisibility = () => {
      if (document.hidden) {
        clearInterval(timer);
      } else {
        fetchData();
        timer = setInterval(fetchData, 5000);
      }
    };
    document.addEventListener("visibilitychange", handleVisibility);

    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [fetchData]);

  const triggerScan = async () => {
    try {
      setIsScanning(true);
      await fetch("/api/trigger_scan", { method: "POST" });
      setTimeout(fetchData, 1000);
    } catch (err) {
      setError("触发扫描失败 Scan Trigger Failed");
    }
  };

  const filteredModels = useMemo(() => {
    return models.filter(m => 
      m.name.toLowerCase().includes(filter.toLowerCase()) || 
      m.provider.toLowerCase().includes(filter.toLowerCase()) ||
      m.id.toLowerCase().includes(filter.toLowerCase())
    );
  }, [models, filter]);

  const groupedModels = useMemo(() => {
    const groups: Record<string, { models: Model[]; avgTTFT: number; count: number }> = {};
    
    filteredModels.forEach(m => {
      if (!groups[m.provider]) {
        groups[m.provider] = { models: [], avgTTFT: 0, count: 0 };
      }
      groups[m.provider].models.push(m);
    });

    // Apply Sorting and Calculate Stats
    Object.keys(groups).forEach(provider => {
      const group = groups[provider];
      
      // Calculate Average TTFT for the provider
      const validPerf = group.models.filter(m => m.performance?.ttft);
      group.avgTTFT = validPerf.length > 0 
        ? validPerf.reduce((acc, curr) => acc + (curr.performance?.ttft || 0), 0) / validPerf.length 
        : 0;
      group.count = group.models.length;

      // Sort models within the group
      group.models.sort((a, b) => {
        if (sortBy === "latency") {
          const latA = a.performance?.ttft || 999;
          const latB = b.performance?.ttft || 999;
          return latA - latB;
        }
        if (sortBy === "context") {
          return (b.context_length || 0) - (a.context_length || 0);
        }
        return a.name.localeCompare(b.name);
      });
    });

    return groups;
  }, [filteredModels, sortBy]);

  const toggleProvider = (provider: string) => {
    const next = new Set(expandedProviders);
    if (next.has(provider)) {
      next.delete(provider);
    } else {
      next.add(provider);
    }
    setExpandedProviders(next);
  };

  const avgTTFT = useMemo(() => {
    const withTTFT = models.filter(m => m.performance?.ttft);
    return withTTFT.length > 0
      ? withTTFT.reduce((acc, curr) => acc + (curr.performance?.ttft || 0), 0) / withTTFT.length
      : 0;
  }, [models]);

  return (
    <main className="min-h-screen bg-[#020617] text-slate-200 selection:bg-cyan-500/30 font-sans">
      {/* Laser Scan Line Overlay */}
      {isScanning && <div className="laser-scan opacity-40" />}

      {/* RENDER WRAPPER */}
      <div className="w-full max-w-[1200px] mx-auto px-6 py-12 md:py-16 flex flex-col">
        
        {/* Header Section: Compact & Technical */}
        <div className="w-full flex flex-col md:flex-row md:items-end justify-between mb-16 gap-8 relative z-10">
          <div className="space-y-4">
            <div className="flex items-center gap-4">
              <div className="p-3 rounded-2xl bg-indigo-500/10 border border-indigo-500/20">
                <Monitor className="text-indigo-400 w-8 h-8" />
              </div>
              <div>
                <h1 className="text-4xl font-black tracking-tighter text-white font-mono">
                  MODEL<span className="text-indigo-500">SCOUT</span>
                </h1>
                <p className="text-[10px] font-bold text-slate-500 uppercase tracking-[0.3em]">
                  Autonomous Intelligence Monitor
                </p>
              </div>
            </div>
          </div>
          
          <div className="flex flex-col md:flex-row items-stretch gap-3 w-full md:w-auto">
            <div className="relative group flex-grow md:w-80">
              <Search className="absolute left-4 top-1/2 -translate-y-1/2 text-slate-500 group-focus-within:text-indigo-400 transition-colors w-4 h-4 shrink-0" />
              <input 
                type="text" 
                placeholder="搜索模型或厂商 Search intelligence…" 
                className="bg-slate-900/60 border border-white/5 rounded-xl py-3 pl-11 pr-4 outline-none focus:ring-1 ring-indigo-500/40 transition-all w-full text-sm font-medium placeholder:text-slate-600 appearance-none"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              />
            </div>
            <button 
              onClick={triggerScan}
              disabled={isScanning}
              className={`flex items-center justify-center gap-3 px-6 py-3 rounded-xl font-bold text-sm transition-all ${
                isScanning 
                  ? 'bg-slate-800 text-slate-500 cursor-not-allowed' 
                  : 'bg-indigo-600 text-white hover:bg-indigo-500 active:scale-95'
              }`}
            >
              {isScanning ? <RefreshCcw className="w-4 h-4 animate-spin" /> : <Zap className="w-4 h-4 fill-current" />}
              <span>{isScanning ? "同步中" : "同步状态"}</span>
            </button>
          </div>
        </div>

        {/* Global Statistics Grid */}
        <div className="w-full grid grid-cols-2 lg:grid-cols-4 gap-4 mb-16 relative z-10">
          <InsightCard 
            icon={<Database className="text-indigo-400 w-4 h-4" />} 
            label="在线资产 Nodes" 
            value={models.length} 
            unit="items"
          />
          <InsightCard 
            icon={<Activity className="text-cyan-400 w-4 h-4" />} 
            label="平均延迟 Latency" 
            value={models.length > 0 ? avgTTFT.toFixed(2) : "---"} 
            unit="sec"
          />
          <InsightCard 
            icon={<ShieldCheck className="text-emerald-400 w-4 h-4" />} 
            label="探测强度 Intensity" 
            value="98.5" 
            unit="%"
          />
          <InsightCard 
            icon={<BarChart3 className="text-amber-400 w-4 h-4" />} 
            label="上次同步 Scanned" 
            value={lastScan ? new Date(lastScan).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'}) : "OFF"} 
            unit=""
          />
        </div>

        {/* Sorting & Filter Controls */}
        <div className="flex items-center gap-3 mb-10 overflow-x-auto pb-2 scrollbar-hide">
           <span className="text-[10px] font-black text-slate-600 uppercase tracking-widest mr-2 shrink-0">Filter:</span>
           {[
             { id: 'name', label: '名称 Name', icon: <Monitor className="w-3 h-3" /> },
             { id: 'latency', label: '延迟 Latency', icon: <Wifi className="w-3 h-3" /> },
             { id: 'context', label: '窗口 Context', icon: <Layers className="w-3 h-3" /> },
           ].map((opt) => (
             <button
               key={opt.id}
               onClick={() => setSortBy(opt.id as "name" | "latency" | "context")}
               className={`px-4 py-1.5 rounded-lg text-[11px] font-bold flex items-center gap-2 transition-all border whitespace-nowrap ${
                 sortBy === opt.id 
                  ? 'bg-indigo-500/10 border-indigo-500/40 text-indigo-300' 
                  : 'bg-transparent border-white/5 text-slate-500 hover:border-white/10 hover:text-slate-300'
               }`}
             >
               {opt.icon}
               {opt.label}
             </button>
           ))}
        </div>

        {error && (
          <div className="w-full mb-10 bg-red-500/10 border border-red-500/20 p-4 rounded-xl flex items-center gap-3 text-red-400">
            <AlertCircle className="w-4 h-4" />
            <p className="text-xs font-bold uppercase tracking-tight">{error}</p>
          </div>
        )}

        {/* Intelligence Clusters (Groups) */}
        <div className="w-full space-y-16 pb-32">
          {Object.keys(groupedModels).sort().map(provider => {
            const isExpanded = expandedProviders.has(provider);
            const data = groupedModels[provider];
            const onlineCount = data.models.filter(m => m.performance?.status === "success").length;
            
            return (
              <section key={provider} className="animate-in fade-in slide-in-from-bottom-2 duration-500">
                {/* Cluster Header */}
                <div 
                  onClick={() => toggleProvider(provider)}
                  className="flex items-center justify-between py-4 mb-6 border-b border-white/5 cursor-pointer group hover:border-indigo-500/30 transition-all"
                >
                  <div className="flex items-center gap-4">
                    <div className="p-2 rounded-lg bg-white/5 group-hover:scale-110 transition-transform">
                      <Cpu className="w-5 h-5" style={{ color: PROVIDER_THEMES[provider] || 'var(--indigo-400)' }} />
                    </div>
                    <div>
                      <h2 className="text-xl font-bold tracking-tight text-white flex items-center gap-3">
                        {provider}
                        <span className="text-[10px] font-black text-slate-600 bg-white/5 px-2 py-0.5 rounded uppercase tracking-widest">Cluster</span>
                      </h2>
                    </div>
                  </div>
                  <div className="flex items-center gap-6">
                    <div className="hidden sm:flex flex-col items-end">
                      <span className="text-xs font-bold text-emerald-400/80">{onlineCount} / {data.count} Active</span>
                    </div>
                    <div className="text-slate-700 group-hover:text-slate-400 transition-colors">
                      {isExpanded ? <ChevronDown className="w-5 h-5" /> : <ChevronRight className="w-5 h-5" />}
                    </div>
                  </div>
                </div>

                {/* Cluster Grid */}
                {isExpanded && (
                  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                    {data.models.map((model) => (
                      <ModelCard key={`${model.provider}-${model.id}`} model={model} isScanning={isScanning} />
                    ))}
                  </div>
                )}
              </section>
            );
          })}

          {filteredModels.length === 0 && (
            <div className="py-32 text-center glass rounded-[3rem] border-dashed border-white/10">
              <Layers className="w-12 h-12 text-slate-800 mx-auto mb-4" />
              <p className="text-slate-500 font-bold uppercase tracking-widest text-sm">信号中断 No Intelligence Found</p>
            </div>
          )}
        </div>
      </div>
    </main>
  );
}

function InsightCard({ icon, label, value, unit }: { 
  icon: ReactNode; 
  label: string; 
  value: string | number; 
  unit: string; 
}) {
  return (
    <div className="glass p-8 group transition-all hover:bg-white/[0.03] border-white/5 shadow-none relative">
      <div className="flex items-center gap-5 mb-6">
        <div className="transition-transform group-hover:scale-110 shrink-0">
          {icon}
        </div>
        <p className="text-slate-500 text-[9px] uppercase font-black tracking-[0.3em]">{label}</p>
      </div>
      <div className="flex items-baseline gap-2">
        <h3 className="text-3xl font-black tracking-tighter tabular-nums text-white leading-none">{value}</h3>
        <span className="text-[10px] font-bold text-slate-700 uppercase italic">{unit}</span>
      </div>
    </div>
  );
}

function ModelCard({ model, isScanning }: { model: Model; isScanning: boolean }) {
  const perf = model.performance;
  const isOnline = perf?.status === "success";
  const isWaiting = isScanning && !perf;

  return (
    <div 
      className={`glass-card group flex flex-col h-full border-white/5 hover:border-white/10 transition-all duration-300 min-h-[320px] rounded-2xl p-6 relative overflow-hidden ${
        isWaiting ? 'bg-slate-900/60' : 'bg-slate-900/40'
      }`}
    >
      {/* Background Pulse during scan */}
      {isWaiting && (
        <div className="absolute inset-0 z-0">
          <div className="absolute inset-0 bg-gradient-to-r from-transparent via-indigo-500/10 to-transparent animate-shimmer" />
        </div>
      )}

      {/* Header: Name & Status */}
      <div className="flex justify-between items-start mb-6 relative z-10">
        <div className="space-y-1">
          <h4 className="text-base font-bold tracking-tight text-white group-hover:text-indigo-300 transition-colors leading-tight">
            {model.name}
          </h4>
          <p className="text-[10px] font-mono text-slate-500 truncate max-w-[140px]">
            {model.id}
          </p>
        </div>
        <div className={`flex items-center gap-2 px-2 py-1 rounded-md border ${
          isWaiting 
            ? 'bg-indigo-500/10 border-indigo-500/20 text-indigo-400' 
            : isOnline 
              ? 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400' 
              : 'bg-slate-800/50 border-white/5 text-slate-600'
        }`}>
          <div className={`w-1.5 h-1.5 rounded-full ${
            isWaiting ? 'bg-indigo-500 animate-pulse' : isOnline ? 'bg-emerald-500' : 'bg-slate-700'
          }`} />
          <span className="text-[9px] font-black tracking-widest uppercase">
            {isWaiting ? "Bench" : isOnline ? "Active" : "Down"}
          </span>
        </div>
      </div>
      
      {/* Metrics Centerpiece */}
      <div className="grid grid-cols-2 gap-4 mb-6 relative z-10">
        <div className="bg-white/2 rounded-xl p-4 border border-white/5 group-hover:border-indigo-500/20 transition-colors">
          <div className="flex items-center gap-2 mb-2">
            <Clock className="w-3 h-3 text-slate-500" />
            <span className="text-[8px] font-black text-slate-500 uppercase tracking-widest">TTFT</span>
          </div>
          <div className="flex items-baseline gap-1">
            <span className={`text-2xl font-black tabular-nums tracking-tighter ${perf?.ttft ? 'text-white' : 'text-slate-800'}`}>
              {perf?.ttft ? perf.ttft : isWaiting ? "---" : "INF"}
            </span>
            {perf?.ttft && <span className="text-[10px] font-bold text-slate-600 uppercase">s</span>}
          </div>
        </div>
        <div className="bg-white/2 rounded-xl p-4 border border-white/5 group-hover:border-indigo-500/20 transition-colors">
          <div className="flex items-center gap-2 mb-2">
            <Activity className="w-3 h-3 text-slate-500" />
            <span className="text-[8px] font-black text-slate-500 uppercase tracking-widest">TPS</span>
          </div>
          <div className="flex items-baseline gap-1">
            <span className={`text-2xl font-black tabular-nums tracking-tighter ${perf?.tps ? 'text-white' : 'text-slate-800'}`}>
              {perf?.tps || (isWaiting ? "---" : "0")}
            </span>
            {perf?.tps && <span className="text-[10px] font-bold text-slate-600 uppercase">T/s</span>}
          </div>
        </div>
      </div>

      {/* Tags & Meta */}
      <div className="flex flex-wrap gap-2 mb-6 relative z-10">
         <span className="bg-slate-900/60 text-[9px] font-bold text-slate-400 px-2 py-1 rounded border border-white/5">
           {model.context_length ? `${Math.round(model.context_length / 1024)}K` : "---"} Context
         </span>
         {model.max_output_tokens && (
           <span className="bg-slate-900/60 text-[9px] font-bold text-slate-400 px-2 py-1 rounded border border-white/5">
             {Math.round(model.max_output_tokens / 1024)}K Output
           </span>
         )}
      </div>

      {/* Brief Description - Truncated for Clarity */}
      <div className="mt-auto pt-4 border-t border-white/5 relative z-10">
        <p className="text-slate-400 text-[11px] leading-snug line-clamp-2">
          {model.description_cn || model.description || "Intelligence telemetry active."}
        </p>
      </div>

      <div className="mt-4 flex items-center justify-between text-[9px] font-bold uppercase tracking-widest text-slate-600 relative z-10">
        <div className="flex items-center gap-2">
          <RefreshCcw className="w-2.5 h-2.5" />
          {perf?.last_checked ? new Date(perf.last_checked).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'}) : "Wait"}
        </div>
        <span>Ready</span>
      </div>
    </div>
  );
}
