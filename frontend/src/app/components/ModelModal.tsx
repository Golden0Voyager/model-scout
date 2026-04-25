"use client";

import { useEffect, useState } from "react";
import {
  X,
  Copy,
  Check,
  RefreshCw,
  Wifi,
  WifiOff,
  Activity,
  HelpCircle,
  Key,
  Database,
  Zap,
  Clock,
  Server,
  AlertCircle,
} from "lucide-react";

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

const STATUS_META: Record<string, { label: string; color: string; icon: React.ReactNode }> = {
  online: { label: "Online", color: "#22c55e", icon: <Wifi className="w-4 h-4" /> },
  offline: { label: "Offline", color: "#ef4444", icon: <WifiOff className="w-4 h-4" /> },
  error: { label: "Error", color: "#f59e0b", icon: <Activity className="w-4 h-4" /> },
  no_key: { label: "No Key", color: "#64748b", icon: <Key className="w-4 h-4" /> },
  unknown: { label: "Unknown", color: "#3b82f6", icon: <HelpCircle className="w-4 h-4" /> },
};

const CAPABILITY_LABELS: Record<string, string> = {
  chat: "Chat",
  coding: "Coding",
  reasoning: "Reasoning",
  vision: "Vision",
  function_calling: "Tools",
  long_context: "Long Context",
  moE: "MoE",
};

const CNY_TO_USD = 7.2;

function latencyColor(ms: number): string {
  if (ms < 200) return "#22c55e";
  if (ms < 500) return "#f59e0b";
  if (ms < 1000) return "#f97316";
  return "#ef4444";
}

function formatContext(n: number): string {
  if (n >= 1000000) return `${(n / 1000000).toFixed(0)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(0)}K`;
  return `${n}`;
}

function formatPrice(input?: number, output?: number, currency?: string): string {
  if (input == null || output == null) return "N/A";
  const cny = currency === "CNY" || !currency;
  if (cny) {
    const inUsd = input / CNY_TO_USD;
    const outUsd = output / CNY_TO_USD;
    return `$${inUsd.toFixed(2)} / $${outUsd.toFixed(2)} per 1M tokens (¥${input.toFixed(2)} / ¥${output.toFixed(2)})`;
  }
  return `$${input.toFixed(2)} / $${output.toFixed(2)} per 1M tokens`;
}

function formatTimeFull(iso?: string): string {
  if (!iso) return "--";
  try {
    const d = new Date(iso);
    return d.toLocaleString("zh-CN", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return iso;
  }
}

interface ModelModalProps {
  model: Model | null;
  onClose: () => void;
  onRefresh?: () => void;
  isScanning?: boolean;
}

export default function ModelModal({ model, onClose, onRefresh, isScanning }: ModelModalProps) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!model) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handler);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", handler);
      document.body.style.overflow = "";
    };
  }, [model, onClose]);

  if (!model) return null;

  const h = model.health;
  const meta = STATUS_META[h.status] || STATUS_META.unknown;

  const copyId = () => {
    navigator.clipboard.writeText(model.id);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      onClick={onClose}
    >
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />

      {/* Modal */}
      <div
        className="relative w-full max-w-lg bg-[#13161f] border border-slate-800 rounded-2xl shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-6 py-5 border-b border-slate-800">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="flex items-center gap-2.5 mb-1">
                <h2 className="text-lg font-bold text-white">{model.name}</h2>
                {model.is_free && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 font-medium">
                    Free
                  </span>
                )}
              </div>
              <div className="flex items-center gap-2">
                <span className="text-xs text-slate-500 font-mono">{model.id}</span>
                <button
                  onClick={copyId}
                  className="text-slate-600 hover:text-slate-300 transition-colors"
                  title="Copy Model ID"
                >
                  {copied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
                </button>
              </div>
            </div>
            <button
              onClick={onClose}
              className="p-1.5 rounded-lg text-slate-500 hover:text-white hover:bg-slate-800 transition-colors"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Body */}
        <div className="px-6 py-5 space-y-5">
          {/* Status bar */}
          <div className="flex items-center gap-3">
            <div
              className="flex items-center gap-2 px-3 py-1.5 rounded-lg border"
              style={{
                borderColor: `${meta.color}25`,
                backgroundColor: `${meta.color}10`,
              }}
            >
              <div className="text-slate-400">{meta.icon}</div>
              <span className="text-sm font-medium" style={{ color: meta.color }}>
                {meta.label}
              </span>
              {h.latency_ms && (
                <span className="text-sm font-medium" style={{ color: latencyColor(h.latency_ms) }}>
                  {h.latency_ms}ms
                </span>
              )}
            </div>
            {onRefresh && (
              <button
                onClick={onRefresh}
                disabled={isScanning}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border transition-all ${
                  isScanning
                    ? "border-indigo-500/20 text-indigo-400 bg-indigo-500/5 cursor-not-allowed"
                    : "border-slate-700 text-slate-400 hover:text-white hover:bg-slate-800 hover:border-slate-600"
                }`}
              >
                <RefreshCw className={`w-3 h-3 ${isScanning ? "animate-spin" : ""}`} />
                {isScanning ? "Checking..." : "Refresh"}
              </button>
            )}
          </div>

          {/* Metrics grid */}
          <div className="grid grid-cols-2 gap-3">
            <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-3">
              <div className="flex items-center gap-2 mb-1.5">
                <Server className="w-3.5 h-3.5 text-slate-500" />
                <span className="text-[11px] text-slate-500 font-medium">Provider</span>
              </div>
              <div className="text-sm text-white font-medium">{model.provider_name}</div>
              <div className="text-[10px] text-slate-600 font-mono mt-0.5">{model.provider}</div>
            </div>
            <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-3">
              <div className="flex items-center gap-2 mb-1.5">
                <Database className="w-3.5 h-3.5 text-slate-500" />
                <span className="text-[11px] text-slate-500 font-medium">Context</span>
              </div>
              <div className="text-sm text-white font-medium">{formatContext(model.context_length)}</div>
              {model.max_output_tokens && (
                <div className="text-[10px] text-slate-500 mt-0.5">
                  Max out: {formatContext(model.max_output_tokens)}
                </div>
              )}
            </div>
          </div>

          {/* Pricing */}
          <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-3">
            <div className="text-[11px] text-slate-500 font-medium mb-1.5">Pricing</div>
            {model.is_free ? (
              <div className="text-sm font-medium text-emerald-400">Free (rate limited)</div>
            ) : (
              <div className="text-sm text-slate-300">
                {formatPrice(model.pricing_input_per_1m, model.pricing_output_per_1m, model.pricing_currency)}
              </div>
            )}
            {model.pricing_note && (
              <div className="text-[11px] text-slate-500 mt-1">{model.pricing_note}</div>
            )}
          </div>

          {/* Capabilities */}
          {model.capabilities.length > 0 && (
            <div>
              <div className="text-[11px] text-slate-500 font-medium mb-2">Capabilities</div>
              <div className="flex flex-wrap gap-1.5">
                {model.capabilities.map((c) => (
                  <span
                    key={c}
                    className="text-[11px] px-2 py-1 rounded-md bg-slate-800 text-slate-300 font-medium"
                  >
                    {CAPABILITY_LABELS[c] || c}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Description */}
          <div>
            <div className="text-[11px] text-slate-500 font-medium mb-1.5">Description</div>
            <p className="text-sm text-slate-300 leading-relaxed">{model.description_cn || model.description}</p>
            {model.description_cn && model.description && model.description !== model.description_cn && (
              <p className="text-xs text-slate-500 leading-relaxed mt-1.5">{model.description}</p>
            )}
          </div>

          {/* Health details */}
          <div className="bg-slate-900/40 border border-slate-800/60 rounded-xl p-3 space-y-2">
            <div className="text-[11px] text-slate-500 font-medium">Health Details</div>
            <div className="flex items-center gap-2 text-xs text-slate-400">
              <Clock className="w-3 h-3 text-slate-600" />
              <span>Last checked: {formatTimeFull(h.last_checked)}</span>
            </div>
            {h.error_message && (
              <div className="flex items-start gap-2 text-xs">
                <AlertCircle className="w-3 h-3 text-amber-500 shrink-0 mt-0.5" />
                <span className="text-amber-400/90 break-all">{h.error_message}</span>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
