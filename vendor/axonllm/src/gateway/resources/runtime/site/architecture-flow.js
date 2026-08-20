(function () {
    'use strict';

    var NODES = [
        { id: 'client-chat', x: 28, y: 80, label: 'Client App', sub: 'OpenAI-compatible SDK', kind: 'client' },
        { id: 'analyst', x: 28, y: 190, label: 'SQL Analyst', sub: 'Governed query request', kind: 'client' },
        { id: 'tenant-user', x: 28, y: 300, label: 'Tenant User', sub: 'Admin or member role', kind: 'client' },

        { id: 'identity', x: 220, y: 50, label: 'Identity Context', sub: 'tenant · project · user', kind: 'govern' },
        { id: 'rbac', x: 220, y: 145, label: 'Tenant RBAC', sub: 'role + scoped action', kind: 'govern' },
        { id: 'quota', x: 220, y: 240, label: 'Quota Reserve', sub: 'rate · budget · tokens', kind: 'govern' },
        { id: 'pii', x: 220, y: 335, label: 'Safety + PII', sub: 'scan · redact · tokenize', kind: 'govern' },

        { id: 'cache', x: 420, y: 50, label: 'Semantic Cache', sub: 'exact then semantic', kind: 'route' },
        { id: 'router', x: 420, y: 145, label: 'Intent Router', sub: 'quality · cost · health', kind: 'route' },
        { id: 'sql', x: 420, y: 240, label: 'SQL Policy', sub: 'parse · SELECT only', kind: 'route' },
        { id: 'response', x: 420, y: 335, label: 'Response Guard', sub: 'validate · re-inject PII', kind: 'route' },

        { id: 'primary', x: 640, y: 30, label: 'Primary Provider', sub: 'best eligible model', kind: 'target' },
        { id: 'fallback', x: 640, y: 110, label: 'Fallback Provider', sub: 'next healthy route', kind: 'target' },
        { id: 'athena', x: 640, y: 190, label: 'Amazon Athena', sub: 'bounded SELECT', kind: 'target' },
        { id: 'config', x: 640, y: 270, label: 'Tenant Config', sub: 'admin write · member view', kind: 'target' },
        { id: 'result', x: 640, y: 350, label: 'Bounded Result', sub: 'rows or completion', kind: 'target' },

        { id: 'audit', x: 850, y: 125, label: 'Audit Chain', sub: 'actor · action · SHA-256', kind: 'observe' },
        { id: 'metrics', x: 850, y: 235, label: 'Cost + Traces', sub: 'usage · latency · provider', kind: 'observe' }
    ];

    var SCENARIOS = [
        {
            id: 'smart-chat',
            name: 'Smart chat routing',
            color: '#7c3aed',
            soft: '#f5f3ff',
            description: 'One OpenAI-shaped request, selected across eligible providers with policy, privacy, caching, and audit controls.',
            path: ['client-chat', 'identity', 'rbac', 'quota', 'pii', 'cache', 'router', 'primary', 'response', 'audit', 'metrics'],
            steps: [
                ['Receive one API shape', 'The application sends POST /v1/chat/completions. It can name a virtual model or leave routing to AxonLLM.'],
                ['Resolve canonical identity', 'Tenant, project, user, environment, and request id are established before any policy decision.'],
                ['Check model access', 'Tenant and project membership are intersected with user model grants. Cross-tenant access is rejected.'],
                ['Reserve quota before billing', 'Rate, token, and spend limits resolve through the hierarchy. The tightest inherited limit wins.'],
                ['Protect the prompt', 'Injection patterns are scored and PII is replaced with request-local tokens before provider egress.'],
                ['Avoid an unnecessary call', 'The exact cache is checked first, then the tenant-scoped semantic cache. A safe hit returns early.'],
                ['Rank eligible models', 'Intent, benchmark quality, real price, provider health, and configured strategy determine the route.'],
                ['Call the selected provider', 'The adapter translates the request into the chosen provider protocol while preserving tool calls and streaming.'],
                ['Guard the response', 'Response rules run and original PII values are re-injected, including across streaming chunk boundaries.'],
                ['Append tamper-evident audit', 'Actor, tenant, policy decision, route, and outcome join the SHA-256 audit chain.'],
                ['Attribute cost and latency', 'Usage, provider, model, cache status, tokens, spend, and trace data fill the dashboard in real time.']
            ]
        },
        {
            id: 'fallback',
            name: 'Provider fallback',
            color: '#047857',
            soft: '#ecfdf5',
            description: 'A direct or smart route fails before bytes are streamed, so AxonLLM retries the next healthy provider without losing governance.',
            path: ['client-chat', 'identity', 'rbac', 'quota', 'router', 'primary', 'fallback', 'response', 'audit', 'metrics'],
            steps: [
                ['Accept the model request', 'The client uses the same endpoint regardless of which provider ultimately serves it.'],
                ['Bind tenant and actor', 'The request receives canonical tenant and project context used by every later control.'],
                ['Authorize the requested model', 'The effective model allow-list is enforced before provider selection.'],
                ['Reserve the maximum exposure', 'Budget and token checks happen before the first external call, not after fallback succeeds.'],
                ['Build a healthy route chain', 'Weight, latency, cost, region, and provider health order the eligible mappings.'],
                ['Try the primary provider', 'A timeout or retryable provider error is recorded. No response bytes have reached the client yet.'],
                ['Fail over transparently', 'The next eligible provider receives the normalized request after bounded retry and backoff.'],
                ['Normalize the answer', 'The successful provider response returns in the same OpenAI-compatible shape. Mid-stream switching is never faked.'],
                ['Record both attempts', 'The audit entry preserves the primary failure, fallback selection, and final outcome.'],
                ['Charge only what happened', 'Cost and traces identify the provider that answered and expose fallback latency to operators.']
            ]
        },
        {
            id: 'governed-sql',
            name: 'Governed SQL SELECT',
            color: '#0369a1',
            soft: '#f0f9ff',
            description: 'A tenant-scoped query is parsed, restricted to SELECT, bounded, executed through Athena, and fully attributed.',
            path: ['analyst', 'identity', 'rbac', 'sql', 'athena', 'result', 'audit', 'metrics'],
            steps: [
                ['Submit a query request', 'The analyst sends SQL and a configured datasource to POST /v1/query.'],
                ['Resolve tenant scope', 'Datasource and project references are interpreted only inside the caller tenant.'],
                ['Require query.select', 'Members may run approved SELECT queries; configuration mutations remain tenant-admin only.'],
                ['Parse and constrain SQL', 'SQLGlot rejects mutation statements, multiple statements, and policy violations before execution.'],
                ['Execute with Athena', 'The approved SELECT runs through the configured workgroup and bounded result settings.'],
                ['Return a bounded result', 'Rows, columns, execution metadata, and reconciliation state return without exposing credentials.'],
                ['Audit actor and statement', 'The immutable record captures who queried which datasource, the decision, and the outcome.'],
                ['Expose operational evidence', 'Latency, status, reconciliation, and usage data are available to the tenant control plane.']
            ]
        },
        {
            id: 'tenant-rbac',
            name: 'Tenant admin RBAC',
            color: '#be123c',
            soft: '#fff1f2',
            description: 'Tenant admins can change configuration. Members can inspect configuration and run allowed chat or SELECT operations, but cannot mutate it.',
            path: ['tenant-user', 'identity', 'rbac', 'config', 'result', 'audit', 'metrics'],
            steps: [
                ['Start with a tenant-scoped session', 'OIDC, bearer token, or API key resolves to one canonical tenant principal.'],
                ['Carry the actor everywhere', 'Tenant id, roles, project memberships, and scopes follow the request; client-supplied authority is not trusted.'],
                ['Evaluate action and role', 'Tenant admins may create or update configuration. Members receive read, chat, and query.select access only.'],
                ['Apply the control-plane decision', 'An allowed admin mutation reaches tenant configuration; a member PUT, POST, or DELETE is denied before the handler.'],
                ['Return an explicit result', 'Allowed reads and writes return normally. Denials identify the missing action without leaking another tenant.'],
                ['Audit every decision', 'Successful changes and denied attempts both record tenant, actor, resource, method, and correlation id.'],
                ['Make access observable', 'Operators can review authorization outcomes, configuration health, and tenant activity from one dashboard.']
            ]
        }
    ];

    var WIDTH = 1020;
    var HEIGHT = 430;
    var NODE_W = 142;
    var NODE_H = 50;
    var instanceCounter = 0;

    function nodeById(id) {
        for (var i = 0; i < NODES.length; i += 1) {
            if (NODES[i].id === id) return NODES[i];
        }
        return null;
    }

    function escapeHtml(value) {
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function pathBetween(fromId, toId) {
        var from = nodeById(fromId);
        var to = nodeById(toId);
        if (!from || !to) return '';
        var x1;
        var y1;
        var x2;
        var y2;
        if (Math.abs(to.x - from.x) < 30) {
            x1 = from.x + NODE_W / 2;
            y1 = from.y + NODE_H;
            x2 = to.x + NODE_W / 2;
            y2 = to.y;
            var midY = (y1 + y2) / 2;
            return 'M' + x1 + ',' + y1 + ' C' + x1 + ',' + midY + ' ' + x2 + ',' + midY + ' ' + x2 + ',' + y2;
        }
        var forward = to.x >= from.x;
        x1 = forward ? from.x + NODE_W : from.x;
        y1 = from.y + NODE_H / 2;
        x2 = forward ? to.x : to.x + NODE_W;
        y2 = to.y + NODE_H / 2;
        var midX = (x1 + x2) / 2;
        return 'M' + x1 + ',' + y1 + ' C' + midX + ',' + y1 + ' ' + midX + ',' + y2 + ' ' + x2 + ',' + y2;
    }

    function icon(name) {
        if (name === 'pause') {
            return '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>';
        }
        if (name === 'volume') {
            return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/></svg>';
        }
        if (name === 'mute') {
            return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 5 6 9H2v6h4l5 4z"/><path d="m16 9 5 5M21 9l-5 5"/></svg>';
        }
        if (name === 'next') {
            return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h13M13 6l6 6-6 6"/></svg>';
        }
        if (name === 'reset') {
            return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg>';
        }
        if (name === 'fullscreen') {
            return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M8 3H3v5M16 3h5v5M8 21H3v-5M16 21h5v-5"/></svg>';
        }
        return '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';
    }

    function Flow(root) {
        this.root = root;
        this.scenarioIndex = 0;
        this.step = 0;
        this.playing = false;
        this.timer = null;
        this.audioEnabled = true;
        this.playbackToken = 0;
        this.id = 'axon-flow-arrow-' + (++instanceCounter);
        this.renderShell();
        this.bind();
        this.updateAudioControl();
        this.selectScenario(0);
        root.dataset.ready = 'true';
    }

    Flow.prototype.renderShell = function () {
        var tabs = SCENARIOS.map(function (scenario, index) {
            return '<button class="axon-flow__scenario" type="button" role="tab" data-scenario="' + index + '" aria-selected="false">' +
                escapeHtml(scenario.name) + '</button>';
        }).join('');
        this.root.innerHTML =
            '<div class="axon-flow__toolbar">' +
                '<div class="axon-flow__scenarios" role="tablist" aria-label="Architecture scenarios">' + tabs + '</div>' +
                '<div class="axon-flow__controls">' +
                    '<button class="axon-flow__control axon-flow__control--play" type="button" data-flow-play aria-label="Play scenario" title="Play scenario">' + icon('play') + '</button>' +
                    '<button class="axon-flow__control axon-flow__control--voice" type="button" data-flow-voice aria-label="Mute narration" aria-pressed="true" title="Mute narration">' + icon('volume') + '</button>' +
                    '<button class="axon-flow__control" type="button" data-flow-next aria-label="Next step" title="Next step">' + icon('next') + '</button>' +
                    '<button class="axon-flow__control" type="button" data-flow-reset aria-label="Reset scenario" title="Reset scenario">' + icon('reset') + '</button>' +
                    '<button class="axon-flow__control" type="button" data-flow-fullscreen aria-label="Enter fullscreen" title="Fullscreen">' + icon('fullscreen') + '</button>' +
                '</div>' +
            '</div>' +
            '<div class="axon-flow__summary">' +
                '<div class="axon-flow__summary-copy"><div class="axon-flow__summary-title" data-flow-title></div><div class="axon-flow__summary-desc" data-flow-description></div></div>' +
                '<div class="axon-flow__status" data-flow-status aria-live="polite"></div>' +
            '</div>' +
            '<div class="axon-flow__canvas-scroll"><svg class="axon-flow__canvas" viewBox="0 0 ' + WIDTH + ' ' + HEIGHT + '" role="img" aria-labelledby="' + this.id + '-title ' + this.id + '-desc" data-flow-svg></svg></div>' +
            '<div class="axon-flow__narrative" aria-live="polite">' +
                '<div class="axon-flow__step-num" data-flow-step-num></div>' +
                '<div class="axon-flow__step-copy"><p class="axon-flow__step-title" data-flow-step-title></p><p class="axon-flow__step-body" data-flow-step-body></p></div>' +
                '<div class="axon-flow__progress" data-flow-progress aria-label="Scenario progress"></div>' +
            '</div>' +
            '<audio data-flow-narration preload="none" aria-hidden="true"></audio>';
        this.playButton = this.root.querySelector('[data-flow-play]');
        this.audioButton = this.root.querySelector('[data-flow-voice]');
        this.audio = this.root.querySelector('[data-flow-narration]');
        this.audio.volume = 0.9;
        this.svg = this.root.querySelector('[data-flow-svg]');
    };

    Flow.prototype.bind = function () {
        var self = this;
        Array.prototype.forEach.call(this.root.querySelectorAll('[data-scenario]'), function (button) {
            button.addEventListener('click', function () {
                self.selectScenario(Number(button.dataset.scenario));
            });
        });
        this.playButton.addEventListener('click', function () {
            if (self.playing) self.pause();
            else self.play();
        });
        this.audioButton.addEventListener('click', function () {
            self.toggleAudio();
        });
        this.root.querySelector('[data-flow-next]').addEventListener('click', function () {
            self.pause(true);
            self.setStep(Math.min(self.current().path.length - 1, self.step + 1));
        });
        this.root.querySelector('[data-flow-reset]').addEventListener('click', function () {
            self.pause(true);
            self.setStep(0);
        });
        this.root.querySelector('[data-flow-fullscreen]').addEventListener('click', function () {
            if (document.fullscreenElement === self.root) {
                document.exitFullscreen();
            } else if (self.root.requestFullscreen) {
                self.root.requestFullscreen();
            }
        });
        document.addEventListener('fullscreenchange', function () {
            var button = self.root.querySelector('[data-flow-fullscreen]');
            var active = document.fullscreenElement === self.root;
            button.setAttribute('aria-label', active ? 'Exit fullscreen' : 'Enter fullscreen');
            button.setAttribute('title', active ? 'Exit fullscreen' : 'Fullscreen');
        });
        window.addEventListener('pagehide', function () {
            self.pause(true);
        });
    };

    Flow.prototype.current = function () {
        return SCENARIOS[this.scenarioIndex];
    };

    Flow.prototype.selectScenario = function (index) {
        this.pause(true);
        this.scenarioIndex = Math.max(0, Math.min(SCENARIOS.length - 1, index));
        this.step = 0;
        var scenario = this.current();
        this.root.style.setProperty('--flow-accent', scenario.color);
        this.root.style.setProperty('--flow-accent-soft', scenario.soft);
        Array.prototype.forEach.call(this.root.querySelectorAll('[data-scenario]'), function (button) {
            var selected = Number(button.dataset.scenario) === index;
            button.setAttribute('aria-selected', selected ? 'true' : 'false');
            button.tabIndex = selected ? 0 : -1;
        });
        this.root.querySelector('[data-flow-title]').textContent = scenario.name;
        this.root.querySelector('[data-flow-description]').textContent = scenario.description;
        this.renderDiagram();
        this.renderProgress();
        this.update();
    };

    Flow.prototype.renderDiagram = function () {
        var scenario = this.current();
        var edges = '';
        for (var i = 1; i < scenario.path.length; i += 1) {
            edges += '<path class="axon-flow__edge" data-edge-index="' + (i - 1) + '" d="' +
                pathBetween(scenario.path[i - 1], scenario.path[i]) + '" marker-end="url(#' + this.id + ')"/>';
        }
        var nodes = NODES.map(function (node) {
            return '<g class="axon-flow__node" data-node="' + node.id + '" data-kind="' + node.kind + '" transform="translate(' + node.x + ' ' + node.y + ')">' +
                '<rect class="axon-flow__node-box" width="' + NODE_W + '" height="' + NODE_H + '" rx="7"/>' +
                '<text class="axon-flow__node-title" x="12" y="21">' + escapeHtml(node.label) + '</text>' +
                '<text class="axon-flow__node-sub" x="12" y="37">' + escapeHtml(node.sub) + '</text>' +
            '</g>';
        }).join('');
        this.svg.innerHTML =
            '<title id="' + this.id + '-title">' + escapeHtml(scenario.name) + '</title>' +
            '<desc id="' + this.id + '-desc">' + escapeHtml(scenario.description) + '</desc>' +
            '<defs><marker id="' + this.id + '" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L8,4 L0,8 Z" fill="var(--flow-accent)"/></marker></defs>' +
            '<rect class="axon-flow__zone" x="14" y="42" width="170" height="360" rx="8"/><text class="axon-flow__zone-label" x="28" y="31">CALLERS</text>' +
            '<rect class="axon-flow__zone" x="204" y="17" width="174" height="395" rx="8"/><text class="axon-flow__zone-label" x="220" y="14">IDENTITY + GOVERNANCE</text>' +
            '<rect class="axon-flow__zone" x="404" y="17" width="174" height="395" rx="8"/><text class="axon-flow__zone-label" x="420" y="14">DECISION PIPELINE</text>' +
            '<rect class="axon-flow__zone" x="624" y="17" width="174" height="395" rx="8"/><text class="axon-flow__zone-label" x="640" y="14">PROVIDERS + DATA</text>' +
            '<rect class="axon-flow__zone" x="834" y="92" width="172" height="226" rx="8"/><text class="axon-flow__zone-label" x="850" y="86">EVIDENCE</text>' +
            edges + nodes +
            '<circle class="axon-flow__packet" r="5" data-flow-packet hidden></circle>';
    };

    Flow.prototype.renderProgress = function () {
        var self = this;
        var progress = this.root.querySelector('[data-flow-progress]');
        progress.innerHTML = this.current().steps.map(function (step, index) {
            return '<button class="axon-flow__dot" type="button" data-flow-dot="' + index + '" aria-label="Go to step ' + (index + 1) + ': ' + escapeHtml(step[0]) + '"></button>';
        }).join('');
        Array.prototype.forEach.call(progress.querySelectorAll('[data-flow-dot]'), function (button) {
            button.addEventListener('click', function () {
                self.pause(true);
                self.setStep(Number(button.dataset.flowDot));
            });
        });
    };

    Flow.prototype.setStep = function (step) {
        this.step = Math.max(0, Math.min(this.current().path.length - 1, step));
        this.update();
    };

    Flow.prototype.update = function () {
        var scenario = this.current();
        var currentNode = scenario.path[this.step];
        var pathPosition = {};
        for (var i = 0; i < scenario.path.length; i += 1) {
            if (pathPosition[scenario.path[i]] === undefined) pathPosition[scenario.path[i]] = i;
        }
        Array.prototype.forEach.call(this.svg.querySelectorAll('[data-node]'), function (node) {
            var position = pathPosition[node.dataset.node];
            node.classList.toggle('is-past', position !== undefined && position < this.step);
            node.classList.toggle('is-current', node.dataset.node === currentNode);
        }, this);
        Array.prototype.forEach.call(this.svg.querySelectorAll('[data-edge-index]'), function (edge) {
            var position = Number(edge.dataset.edgeIndex);
            edge.classList.toggle('is-past', position < this.step - 1);
            edge.classList.toggle('is-current', position === this.step - 1);
        }, this);

        var packet = this.svg.querySelector('[data-flow-packet]');
        packet.innerHTML = '';
        if (this.step > 0) {
            var motionPath = pathBetween(scenario.path[this.step - 1], scenario.path[this.step]);
            packet.hidden = false;
            packet.innerHTML = '<animateMotion dur="1.15s" repeatCount="indefinite" path="' + motionPath + '"/>';
        } else {
            packet.hidden = true;
        }

        var copy = scenario.steps[this.step];
        this.root.querySelector('[data-flow-step-num]').textContent = String(this.step + 1).padStart(2, '0');
        this.root.querySelector('[data-flow-step-title]').textContent = copy[0];
        this.root.querySelector('[data-flow-step-body]').textContent = copy[1];
        this.root.querySelector('[data-flow-status]').textContent = 'STEP ' + (this.step + 1) + ' / ' + scenario.steps.length;
        Array.prototype.forEach.call(this.root.querySelectorAll('[data-flow-dot]'), function (dot) {
            var position = Number(dot.dataset.flowDot);
            dot.classList.toggle('is-past', position < this.step);
            dot.classList.toggle('is-current', position === this.step);
        }, this);
    };

    Flow.prototype.updateAudioControl = function () {
        if (!this.audioButton) return;
        this.audioButton.innerHTML = icon(this.audioEnabled ? 'volume' : 'mute');
        this.audioButton.setAttribute('aria-pressed', this.audioEnabled ? 'true' : 'false');
        this.audioButton.setAttribute('aria-label', this.audioEnabled ? 'Mute narration' : 'Enable narration');
        this.audioButton.setAttribute('title', this.audioEnabled ? 'Mute narration' : 'Enable narration');
        if (!this.playing) {
            this.root.dataset.narrationState = this.audioEnabled ? 'ready' : 'muted';
        }
    };

    Flow.prototype.readingDuration = function () {
        var copy = this.current().steps[this.step];
        var words = (copy[0] + ' ' + copy[1]).trim().split(/\s+/).length;
        return Math.max(3200, Math.min(10000, Math.round((words / 2.7) * 1000 + 500)));
    };

    Flow.prototype.clearPlayback = function (rewind) {
        this.playbackToken += 1;
        if (this.timer) window.clearTimeout(this.timer);
        this.timer = null;
        if (!this.audio) return;
        this.audio.pause();
        if (rewind && this.audio.readyState > 0) {
            try {
                this.audio.currentTime = 0;
            } catch (error) {
                // A source switched before metadata loaded has nothing to seek.
            }
        }
    };

    Flow.prototype.scheduleSilentAdvance = function (token, unavailable) {
        var self = this;
        if (!this.playing || token !== this.playbackToken || this.timer) return;
        this.root.dataset.narrationState = unavailable ? 'unavailable' : 'muted';
        this.timer = window.setTimeout(function () {
            self.timer = null;
            if (self.playing && token === self.playbackToken) self.advance();
        }, this.readingDuration());
    };

    Flow.prototype.playCurrentNarration = function () {
        var self = this;
        this.clearPlayback(false);
        var token = this.playbackToken;
        if (!this.audioEnabled) {
            this.scheduleSilentAdvance(token, false);
            return;
        }

        var scenario = this.current();
        var source = new URL(
            'narration/' + scenario.id + '-' + this.step + '.mp3',
            document.baseURI
        ).href;
        var sameSource = this.audio.src === source;
        if (!sameSource) {
            this.audio.src = source;
            this.audio.load();
        } else if (this.audio.ended) {
            this.audio.currentTime = 0;
        }

        this.root.dataset.narrationState = 'loading';
        this.audio.onplaying = function () {
            if (self.playing && token === self.playbackToken) {
                self.root.dataset.narrationState = 'playing';
            }
        };
        this.audio.onended = function () {
            if (self.playing && token === self.playbackToken) self.advance();
        };
        this.audio.onerror = function () {
            self.scheduleSilentAdvance(token, true);
        };
        var attempt = this.audio.play();
        if (attempt && typeof attempt.catch === 'function') {
            attempt.catch(function () {
                self.scheduleSilentAdvance(token, true);
            });
        }
    };

    Flow.prototype.advance = function () {
        if (!this.playing) return;
        if (this.step >= this.current().path.length - 1) {
            this.pause(true);
            this.root.dataset.narrationState = 'complete';
            return;
        }
        this.setStep(this.step + 1);
        this.playCurrentNarration();
    };

    Flow.prototype.play = function () {
        if (this.step >= this.current().path.length - 1) this.setStep(0);
        this.playing = true;
        this.playButton.innerHTML = icon('pause');
        this.playButton.setAttribute('aria-label', 'Pause scenario');
        this.playButton.setAttribute('title', 'Pause scenario');
        this.playCurrentNarration();
    };

    Flow.prototype.pause = function (rewind) {
        this.playing = false;
        this.clearPlayback(!!rewind);
        if (!this.playButton) return;
        this.playButton.innerHTML = icon('play');
        this.playButton.setAttribute('aria-label', 'Play scenario');
        this.playButton.setAttribute('title', 'Play scenario');
        this.root.dataset.narrationState = this.audioEnabled ? 'paused' : 'muted';
    };

    Flow.prototype.toggleAudio = function () {
        this.audioEnabled = !this.audioEnabled;
        this.clearPlayback(true);
        this.updateAudioControl();
        if (this.playing) this.playCurrentNarration();
    };

    function init() {
        Array.prototype.forEach.call(document.querySelectorAll('[data-axon-flow]'), function (root) {
            if (!root.dataset.ready) new Flow(root);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
