import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  ArrowRight,
  Boxes,
  ChevronLeft,
  ChevronRight,
  Cloud,
  Database,
  Network,
  Pause,
  Play,
  RotateCcw,
  Server,
  ShieldCheck,
  Volume2,
  VolumeX,
  Workflow,
} from "lucide-react";
import {
  DEPLOYMENT_VIEWS,
  GATE_CHAIN,
  PROFILE_NAMES,
  RUNTIME_SCENARIOS,
  type ArchitectureLane,
} from "../lib/architecture";

type ViewMode = "runtime" | "deployment";

const LANE_STYLES: Record<ArchitectureLane, { label: string; className: string }> = {
  client: { label: "Agent / SDK", className: "border-sky-200 bg-sky-50 text-sky-800" },
  gateway: { label: "Gateway", className: "border-amber-200 bg-amber-50 text-amber-800" },
  control: { label: "Control plane", className: "border-violet-200 bg-violet-50 text-violet-800" },
  target: { label: "Target", className: "border-emerald-200 bg-emerald-50 text-emerald-800" },
};

const TOPOLOGY_COLUMNS = [
  { key: "ingress", label: "Ingress", icon: Network, className: "border-sky-200 bg-sky-50/60 text-sky-800" },
  { key: "compute", label: "Compute", icon: Server, className: "border-amber-200 bg-amber-50/60 text-amber-800" },
  { key: "state", label: "State", icon: Database, className: "border-emerald-200 bg-emerald-50/60 text-emerald-800" },
  { key: "controls", label: "Controls", icon: ShieldCheck, className: "border-violet-200 bg-violet-50/60 text-violet-800" },
] as const;

const NARRATION_STORAGE_KEY = "ostiari-architecture-narration";
const configuredPlaybackDelay = Number(import.meta.env.VITE_ARCHITECTURE_STEP_MS);
const PLAYBACK_DELAY_MS = Number.isFinite(configuredPlaybackDelay) && configuredPlaybackDelay > 0
  ? configuredPlaybackDelay
  : 1800;

export function Architecture() {
  const [viewMode, setViewMode] = useState<ViewMode>("runtime");
  const [scenarioIndex, setScenarioIndex] = useState(0);
  const [stepIndex, setStepIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [deploymentIndex, setDeploymentIndex] = useState(0);
  const [audioEnabled, setAudioEnabled] = useState(true);
  const [audioAvailable, setAudioAvailable] = useState(true);
  const narrationTokenRef = useRef(0);
  const activeStepRef = useRef<HTMLButtonElement | null>(null);

  const scenario = RUNTIME_SCENARIOS[scenarioIndex];
  const activeStep = scenario.steps[stepIndex];
  const deployment = DEPLOYMENT_VIEWS[deploymentIndex];

  const cancelNarration = useCallback(() => {
    narrationTokenRef.current += 1;
    if ("speechSynthesis" in window) {
      window.speechSynthesis.cancel();
    }
  }, []);

  const narrateActiveStep = useCallback((onComplete: () => void) => {
    let cancelled = false;
    let fallbackTimer: number | undefined;
    const finish = () => {
      if (cancelled) return;
      cancelled = true;
      if (fallbackTimer !== undefined) window.clearTimeout(fallbackTimer);
      onComplete();
    };

    if (!audioEnabled || !audioAvailable) {
      fallbackTimer = window.setTimeout(finish, PLAYBACK_DELAY_MS);
      return () => {
        cancelled = true;
        if (fallbackTimer !== undefined) window.clearTimeout(fallbackTimer);
      };
    }

    const token = narrationTokenRef.current + 1;
    narrationTokenRef.current = token;
    window.speechSynthesis.cancel();

    const utterance = new SpeechSynthesisUtterance(
      `${activeStep.title}. ${activeStep.detail}`,
    );
    utterance.rate = 1.02;
    utterance.pitch = 1;
    utterance.volume = 0.9;
    const preferredVoice = window.speechSynthesis.getVoices().find(
      (voice) => voice.lang.toLowerCase().startsWith("en") && voice.localService,
    );
    if (preferredVoice) utterance.voice = preferredVoice;

    utterance.onend = () => {
      if (narrationTokenRef.current === token) finish();
    };
    utterance.onerror = () => {
      if (narrationTokenRef.current === token) finish();
    };

    document.dispatchEvent(new CustomEvent("ostiari:architecture-audio", {
      detail: {
        scenario: scenario.id,
        step: stepIndex,
        text: utterance.text,
      },
    }));
    window.speechSynthesis.resume();
    window.speechSynthesis.speak(utterance);
    fallbackTimer = window.setTimeout(finish, 30_000);

    return () => {
      cancelled = true;
      if (fallbackTimer !== undefined) window.clearTimeout(fallbackTimer);
      if (narrationTokenRef.current === token) cancelNarration();
    };
  }, [
    activeStep.detail,
    activeStep.title,
    audioAvailable,
    audioEnabled,
    cancelNarration,
    scenario.id,
    stepIndex,
  ]);

  useEffect(() => {
    const supported = "speechSynthesis" in window && "SpeechSynthesisUtterance" in window;
    setAudioAvailable(supported);
    const stored = window.localStorage.getItem(NARRATION_STORAGE_KEY);
    if (stored === "off") setAudioEnabled(false);
    return cancelNarration;
  }, [cancelNarration]);

  useEffect(() => {
    if (!playing) return undefined;
    return narrateActiveStep(() => {
      if (stepIndex >= scenario.steps.length - 1) {
        setPlaying(false);
        return;
      }
      setStepIndex((current) => current + 1);
    });
  }, [narrateActiveStep, playing, scenario.steps.length, stepIndex]);

  useEffect(() => {
    if (!playing) return;
    activeStepRef.current?.scrollIntoView({
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
        ? "auto"
        : "smooth",
      block: "nearest",
      inline: "center",
    });
  }, [playing, stepIndex]);

  const selectScenario = (index: number) => {
    cancelNarration();
    setScenarioIndex(index);
    setStepIndex(0);
    setPlaying(false);
  };

  const resetRuntime = () => {
    cancelNarration();
    setPlaying(false);
    setStepIndex(0);
  };

  const selectRuntimeStep = (index: number) => {
    cancelNarration();
    setPlaying(false);
    setStepIndex(index);
  };

  const togglePlayback = () => {
    if (playing) {
      cancelNarration();
      setPlaying(false);
      return;
    }
    if (stepIndex >= scenario.steps.length - 1) setStepIndex(0);
    if ("speechSynthesis" in window) window.speechSynthesis.resume();
    setPlaying(true);
  };

  const toggleAudio = () => {
    const next = !audioEnabled;
    setAudioEnabled(next);
    window.localStorage.setItem(NARRATION_STORAGE_KEY, next ? "on" : "off");
    if (!next) cancelNarration();
  };

  const selectViewMode = (mode: ViewMode) => {
    if (mode !== "runtime") {
      cancelNarration();
      setPlaying(false);
    }
    setViewMode(mode);
  };

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="text-xs font-semibold uppercase text-violet-700">Current system architecture</p>
          <h1 className="mt-1 text-2xl font-bold text-stone-900">Ostiari runtime and deployment</h1>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-stone-600">
            Follow a governed request through the gateway, or inspect the topology and controls for every supported deployment profile.
          </p>
        </div>
        <div className="inline-flex rounded-lg border border-stone-200 bg-white p-1 shadow-sm" role="tablist" aria-label="Architecture view">
          <button
            type="button"
            role="tab"
            aria-selected={viewMode === "runtime"}
            data-testid="architecture-view-runtime"
            onClick={() => selectViewMode("runtime")}
            className={`inline-flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium transition ${
              viewMode === "runtime" ? "bg-stone-900 text-white" : "text-stone-600 hover:bg-stone-50"
            }`}
          >
            <Workflow className="h-4 w-4" />
            Runtime flow
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={viewMode === "deployment"}
            data-testid="architecture-view-deployment"
            onClick={() => selectViewMode("deployment")}
            className={`inline-flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium transition ${
              viewMode === "deployment" ? "bg-stone-900 text-white" : "text-stone-600 hover:bg-stone-50"
            }`}
          >
            <Cloud className="h-4 w-4" />
            Deployment
          </button>
        </div>
      </header>

      {viewMode === "runtime" ? (
        <>
          <section
            className="rounded-lg border border-stone-200 bg-white shadow-sm"
            data-testid="architecture-runtime"
            data-playing={playing}
          >
            <div className="border-b border-stone-100 p-4 sm:p-5">
              <div className="flex gap-2 overflow-x-auto pb-1" role="tablist" aria-label="Runtime scenario">
                {RUNTIME_SCENARIOS.map((item, index) => (
                  <button
                    key={item.id}
                    type="button"
                    role="tab"
                    aria-selected={scenarioIndex === index}
                    data-scenario-id={item.id}
                    onClick={() => selectScenario(index)}
                    className={`shrink-0 rounded-md border px-3 py-2 text-left transition ${
                      scenarioIndex === index
                        ? "border-stone-900 bg-stone-900 text-white"
                        : "border-stone-200 bg-white text-stone-600 hover:bg-stone-50"
                    }`}
                  >
                    <span className="block text-xs font-semibold">{item.name}</span>
                    <span className={`mt-0.5 block font-mono text-[10px] ${
                      scenarioIndex === index ? "text-stone-300" : "text-stone-400"
                    }`}>
                      {item.endpoint}
                    </span>
                  </button>
                ))}
              </div>
            </div>

            <div className="p-4 sm:p-6">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="max-w-3xl">
                  <div className="flex items-center gap-2">
                    <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: scenario.accent }} />
                    <h2 className="text-lg font-bold text-stone-900">{scenario.name}</h2>
                  </div>
                  <p className="mt-2 text-sm leading-6 text-stone-600">{scenario.summary}</p>
                </div>
                <div className="flex flex-wrap items-center gap-1 rounded-lg border border-stone-200 bg-stone-50 p-1">
                  <button
                    type="button"
                    onClick={toggleAudio}
                    disabled={!audioAvailable}
                    aria-pressed={audioEnabled}
                    className={`inline-flex items-center gap-1.5 rounded-md px-2.5 py-2 text-xs font-semibold transition disabled:cursor-not-allowed disabled:opacity-40 ${
                      audioEnabled
                        ? "bg-emerald-50 text-emerald-700 hover:bg-emerald-100"
                        : "text-stone-500 hover:bg-white"
                    }`}
                    title={
                      audioAvailable
                        ? audioEnabled ? "Turn narration off" : "Turn narration on"
                        : "Narration is unavailable in this browser"
                    }
                  >
                    {audioEnabled ? <Volume2 className="h-4 w-4" /> : <VolumeX className="h-4 w-4" />}
                    <span className="hidden sm:inline">
                      {audioEnabled ? "Narration on" : "Narration off"}
                    </span>
                  </button>
                  <button
                    type="button"
                    onClick={() => selectRuntimeStep(Math.max(0, stepIndex - 1))}
                    disabled={stepIndex === 0}
                    className="rounded-md p-2 text-stone-600 transition hover:bg-white disabled:opacity-30"
                    title="Previous step"
                    aria-label="Previous step"
                  >
                    <ChevronLeft className="h-4 w-4" />
                  </button>
                  <button
                    type="button"
                    onClick={togglePlayback}
                    className="rounded-md bg-stone-900 p-2 text-white transition hover:bg-stone-700"
                    title={playing ? "Pause flow" : "Play flow"}
                    aria-label={playing ? "Pause flow" : "Play flow"}
                  >
                    {playing ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
                  </button>
                  <button
                    type="button"
                    onClick={resetRuntime}
                    className="rounded-md p-2 text-stone-600 transition hover:bg-white"
                    title="Reset flow"
                    aria-label="Reset flow"
                  >
                    <RotateCcw className="h-4 w-4" />
                  </button>
                  <button
                    type="button"
                    onClick={() => selectRuntimeStep(Math.min(scenario.steps.length - 1, stepIndex + 1))}
                    disabled={stepIndex === scenario.steps.length - 1}
                    className="rounded-md p-2 text-stone-600 transition hover:bg-white disabled:opacity-30"
                    title="Next step"
                    aria-label="Next step"
                  >
                    <ChevronRight className="h-4 w-4" />
                  </button>
                </div>
              </div>

              <div className="mt-5" aria-label={`Flow progress: step ${stepIndex + 1} of ${scenario.steps.length}`}>
                <div className="mb-1.5 flex items-center justify-between text-[10px] font-semibold uppercase tracking-wide text-stone-400">
                  <span>Flow progress</span>
                  <span>{stepIndex + 1} / {scenario.steps.length}</span>
                </div>
                <div className="h-1.5 overflow-hidden rounded-full bg-stone-100">
                  <div
                    className="architecture-progress h-full rounded-full"
                    style={{
                      backgroundColor: scenario.accent,
                      width: `${((stepIndex + 1) / scenario.steps.length) * 100}%`,
                    }}
                  />
                </div>
              </div>

              <div className="mt-6 overflow-x-auto pb-2">
                <div className="flex min-w-max items-stretch gap-2">
                  {scenario.steps.map((step, index) => {
                    const lane = LANE_STYLES[step.lane];
                    const isActive = index === stepIndex;
                    const isComplete = index < stepIndex;
                    return (
                      <div key={`${scenario.id}-${step.title}`} className="flex items-center gap-2">
                        <button
                          ref={isActive ? activeStepRef : undefined}
                          type="button"
                          onClick={() => selectRuntimeStep(index)}
                          aria-current={isActive ? "step" : undefined}
                          data-step-index={index}
                          className={`w-40 rounded-lg border p-3 text-left transition ${
                            isActive
                              ? `${lane.className} shadow-sm ring-2 ring-stone-900 ring-offset-2 ${
                                  playing ? "architecture-flow-active" : ""
                                }`
                              : isComplete
                                ? "border-stone-300 bg-stone-100 text-stone-700"
                                : "border-stone-200 bg-white text-stone-500 hover:bg-stone-50"
                          }`}
                        >
                          <span className="block text-[10px] font-semibold uppercase">
                            {index + 1} · {lane.label}
                          </span>
                          <span className="mt-1 block text-xs font-semibold leading-5">{step.title}</span>
                        </button>
                        {index < scenario.steps.length - 1 && (
                          <span className={
                            playing && stepIndex === index + 1
                              ? "architecture-flow-arrow-active"
                              : ""
                          }>
                            <ArrowRight className={`h-4 w-4 shrink-0 ${isComplete ? "text-stone-600" : "text-stone-300"}`} />
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>

              <div className="mt-5 grid gap-4 lg:grid-cols-[minmax(0,1fr)_auto]">
                <div
                  key={`${scenario.id}-${stepIndex}`}
                  className={`architecture-detail-enter rounded-lg border p-5 ${LANE_STYLES[activeStep.lane].className}`}
                  aria-live="polite"
                  data-testid="architecture-step-detail"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="text-xs font-semibold uppercase">
                      Step {stepIndex + 1} of {scenario.steps.length} · {LANE_STYLES[activeStep.lane].label}
                    </p>
                    {activeStep.contract && (
                      <code className="rounded bg-white/70 px-2 py-1 text-[11px]">{activeStep.contract}</code>
                    )}
                  </div>
                  <h3 className="mt-2 text-base font-bold">{activeStep.title}</h3>
                  <p className="mt-2 text-sm leading-6">{activeStep.detail}</p>
                </div>
                <div className="flex flex-wrap content-start gap-2 lg:w-56">
                  {(Object.keys(LANE_STYLES) as ArchitectureLane[]).map((laneName) => (
                    <span
                      key={laneName}
                      className={`inline-flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-xs font-medium ${LANE_STYLES[laneName].className}`}
                    >
                      <span className="h-2 w-2 rounded-full bg-current opacity-60" />
                      {LANE_STYLES[laneName].label}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          </section>

          <section>
            <div className="flex items-center gap-2">
              <ShieldCheck className="h-5 w-5 text-violet-700" />
              <h2 className="text-lg font-bold text-stone-900">Canonical direct-tool gate chain</h2>
            </div>
            <p className="mt-1 text-sm text-stone-500">
              A2A adds delegation checks before authorization. Intervene-tier calls add human approval before payment and execution.
            </p>
            <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              {GATE_CHAIN.map((gate, index) => (
                <div key={gate.label} className="rounded-lg border border-stone-200 bg-white p-4 shadow-sm">
                  <div className="flex items-center gap-2">
                    <span className="flex h-6 w-6 items-center justify-center rounded-full bg-stone-900 text-xs font-bold text-white">
                      {index + 1}
                    </span>
                    <h3 className="text-sm font-bold text-stone-900">{gate.label}</h3>
                  </div>
                  <p className="mt-2 text-xs leading-5 text-stone-500">{gate.detail}</p>
                </div>
              ))}
            </div>
          </section>
        </>
      ) : (
        <>
          <section
            className="rounded-lg border border-stone-200 bg-white shadow-sm"
            data-testid="architecture-deployment"
          >
            <div className="border-b border-stone-100 p-4 sm:p-5">
              <div className="flex gap-2 overflow-x-auto pb-1" role="tablist" aria-label="Deployment topology">
                {DEPLOYMENT_VIEWS.map((item, index) => (
                  <button
                    key={item.id}
                    type="button"
                    role="tab"
                    aria-selected={deploymentIndex === index}
                    data-deployment-id={item.id}
                    onClick={() => setDeploymentIndex(index)}
                    className={`shrink-0 rounded-md border px-3 py-2 text-sm font-semibold transition ${
                      deploymentIndex === index
                        ? "border-stone-900 bg-stone-900 text-white"
                        : "border-stone-200 bg-white text-stone-600 hover:bg-stone-50"
                    }`}
                  >
                    {item.name}
                  </button>
                ))}
              </div>
            </div>

            <div className="p-4 sm:p-6">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="max-w-3xl">
                  <div className="flex items-center gap-2">
                    <Boxes className="h-5 w-5 text-violet-700" />
                    <h2 className="text-lg font-bold text-stone-900">{deployment.name}</h2>
                  </div>
                  <p className="mt-2 text-sm leading-6 text-stone-600">{deployment.summary}</p>
                </div>
                <div className="flex flex-wrap gap-2">
                  {deployment.profiles.map((profile) => (
                    <code key={profile} className="rounded-md border border-stone-200 bg-stone-50 px-2.5 py-1.5 text-xs text-stone-700">
                      {profile}
                    </code>
                  ))}
                </div>
              </div>

              <div className="mt-6 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                {TOPOLOGY_COLUMNS.map((column, index) => {
                  const Icon = column.icon;
                  const values = deployment[column.key];
                  return (
                    <div
                      key={`${deployment.id}-${column.key}`}
                      className="architecture-topology-enter relative"
                      style={{ animationDelay: `${index * 70}ms` }}
                    >
                      <div className={`h-full rounded-lg border p-4 ${column.className}`}>
                        <div className="flex items-center gap-2">
                          <Icon className="h-4 w-4" />
                          <h3 className="text-sm font-bold">{column.label}</h3>
                        </div>
                        <ul className="mt-3 space-y-2">
                          {values.map((value) => (
                            <li key={value} className="flex gap-2 text-xs leading-5">
                              <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-current opacity-60" />
                              <span>{value}</span>
                            </li>
                          ))}
                        </ul>
                      </div>
                      {index < TOPOLOGY_COLUMNS.length - 1 && (
                        <ArrowRight className="absolute -right-3 top-1/2 z-10 hidden h-5 w-5 -translate-y-1/2 rounded-full bg-white text-stone-400 xl:block" />
                      )}
                    </div>
                  );
                })}
              </div>

              {deployment.agentcore && (
                <div className="mt-4 rounded-lg border border-sky-200 bg-sky-50 p-4">
                  <div className="flex items-center gap-2 text-sky-800">
                    <Activity className="h-4 w-4" />
                    <h3 className="text-sm font-bold">AgentCore runtime details</h3>
                  </div>
                  <ul className="mt-2 grid gap-2 text-xs leading-5 text-sky-800 md:grid-cols-3">
                    {deployment.agentcore.map((detail) => (
                      <li key={detail}>{detail}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          </section>

          <section className="rounded-lg border border-stone-200 bg-white p-4 shadow-sm sm:p-5">
            <h2 className="text-lg font-bold text-stone-900">Supported launcher profiles</h2>
            <p className="mt-1 text-sm text-stone-500">Every supported profile is represented by exactly one topology above.</p>
            <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
              {PROFILE_NAMES.map((profile) => {
                const owner = DEPLOYMENT_VIEWS.find((view) => view.profiles.includes(profile));
                return (
                  <div key={profile} className="rounded-md border border-stone-200 bg-stone-50 px-3 py-2.5">
                    <code className="text-xs font-semibold text-stone-800">{profile}</code>
                    <p className="mt-1 text-[11px] text-stone-500">{owner?.name}</p>
                  </div>
                );
              })}
            </div>
          </section>
        </>
      )}
    </div>
  );
}
