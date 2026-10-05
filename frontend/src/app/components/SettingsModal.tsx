"use client";

import { useCallback, useEffect, useState } from "react";
import { X, RefreshCw, EyeOff, AlertCircle } from "lucide-react";

interface ProviderSetting {
  key: string;
  name: string;
  enabled: boolean;
  default_enabled: boolean;
  model_count: number;
  online_count: number;
}

interface SettingsModalProps {
  open: boolean;
  onClose: () => void;
  /** Lets the dashboard re-poll right after a switch moves. */
  onChanged?: () => void;
}

export default function SettingsModal({ open, onClose, onChanged }: SettingsModalProps) {
  const [providers, setProviders] = useState<ProviderSetting[] | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handler);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", handler);
      document.body.style.overflow = "";
    };
  }, [open, onClose]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setError(null);
    fetch("/api/providers")
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res.status}`))))
      .then((json: { providers: ProviderSetting[] }) => {
        if (!cancelled) setProviders(json.providers);
      })
      .catch(() => {
        if (!cancelled) setError("Could not load providers");
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const toggle = useCallback(
    async (key: string, enabled: boolean) => {
      setPending(key);
      setError(null);
      try {
        const res = await fetch(`/api/providers/${key}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json: { providers: ProviderSetting[] } = await res.json();
        // The server returns the whole list, so a switch settles on its stored value
        // instead of on what this click assumed.
        setProviders(json.providers);
        onChanged?.();
      } catch {
        setError(`Could not update ${key}`);
      } finally {
        setPending(null);
      }
    },
    [onChanged]
  );

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" onClick={onClose}>
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />

      <div
        className="relative w-full max-w-lg bg-[#13161f] border border-slate-800 rounded-2xl shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-6 py-5 border-b border-slate-800 flex items-start justify-between gap-4">
          <div>
            <h2 className="text-lg font-bold text-white">Provider Settings</h2>
            <p className="text-xs text-slate-500 mt-1">
              Switched-off providers are hidden from the dashboard and skipped by every scan.
            </p>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg text-slate-500 hover:text-white hover:bg-slate-800 transition-colors"
            aria-label="Close settings"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="px-6 py-4 max-h-[60vh] overflow-y-auto">
          {error && (
            <div className="mb-3 flex items-center gap-2 bg-red-500/10 border border-red-500/20 px-3 py-2 rounded-lg text-red-400 text-xs">
              <AlertCircle className="w-3.5 h-3.5 shrink-0" />
              {error}
            </div>
          )}

          {!providers && !error && (
            <div className="flex items-center gap-2 text-slate-500 text-sm py-6">
              <RefreshCw className="w-4 h-4 animate-spin" />
              Loading providers...
            </div>
          )}

          {providers && (
            <ul className="space-y-1">
              {providers.map((p) => (
                <li
                  key={p.key}
                  className="flex items-center justify-between gap-3 py-2.5 px-3 -mx-3 rounded-lg hover:bg-slate-800/40 transition-colors"
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-white truncate">{p.name}</span>
                      {!p.enabled && (
                        <span className="flex items-center gap-1 text-[10px] text-slate-500 shrink-0">
                          <EyeOff className="w-3 h-3" />
                          Hidden
                        </span>
                      )}
                      {p.enabled !== p.default_enabled && (
                        <span className="text-[9px] px-1 py-0.5 rounded bg-indigo-500/10 text-indigo-300 font-medium shrink-0">
                          {p.default_enabled ? "off by default" : "on by default"}
                        </span>
                      )}
                    </div>
                    <div className="text-[11px] text-slate-500 font-mono mt-0.5">
                      {p.key} · {p.online_count}/{p.model_count} online
                    </div>
                  </div>
                  <button
                    role="switch"
                    aria-checked={p.enabled}
                    aria-label={`Toggle ${p.name}`}
                    disabled={pending === p.key}
                    onClick={() => toggle(p.key, !p.enabled)}
                    className={`relative shrink-0 w-10 h-6 rounded-full transition-colors ${
                      p.enabled ? "bg-indigo-600" : "bg-slate-700"
                    } ${pending === p.key ? "opacity-50 cursor-wait" : "hover:opacity-90"}`}
                  >
                    <span
                      className={`absolute top-1 w-4 h-4 rounded-full bg-white transition-all ${
                        p.enabled ? "left-5" : "left-1"
                      }`}
                    />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="px-6 py-3 border-t border-slate-800 text-[11px] text-slate-500">
          Turning a provider on starts a full sync, so the panel refills itself.
        </div>
      </div>
    </div>
  );
}
