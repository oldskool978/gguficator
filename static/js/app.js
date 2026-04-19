document.addEventListener('DOMContentLoaded', () => {
    // --- State Management ---
    let authToken = localStorage.getItem('hf_token') || null;
    let selectedRepoId = null;
    let currentSearchData = [];
    let currentPage = 1;
    const itemsPerPage = 5;

    // --- Dynamic Styles for In-Process Indicators & Badges ---
    const style = document.createElement('style');
    style.textContent = `
        @keyframes spin-anim { 100% { transform: rotate(360deg); } }
        .spin-svg { animation: spin-anim 2s linear infinite; }
        .arch-badge {
            font-size: 0.65rem; font-family: var(--font-mono); padding: 0.15rem 0.5rem; 
            border-radius: var(--radius-sm); font-weight: 700; text-transform: uppercase;
            border: 1px solid; display: inline-flex; align-items: center; gap: 0.25rem;
        }
    `;
    document.head.appendChild(style);

    // --- DOM Anchors ---
    const dom = {
        authForm: document.getElementById('auth-form'),
        tokenInput: document.getElementById('token-input'),
        btnAuth: document.getElementById('btn-auth'),
        authStatus: document.getElementById('auth-status'),
        searchInput: document.getElementById('search-input'),
        filterSort: document.getElementById('filter-sort'),
        btnSearch: document.getElementById('btn-search'),
        searchResults: document.getElementById('search-results'),
        paginationModule: document.getElementById('pagination-module'),
        btnPrevPage: document.getElementById('btn-prev-page'),
        btnNextPage: document.getElementById('btn-next-page'),
        pageIndicator: document.getElementById('page-indicator'),
        quantSelect: document.getElementById('quant-select'),
        btnExecute: document.getElementById('btn-execute'),
        terminalOutput: document.getElementById('terminal-output'),
        connectionStatus: document.getElementById('connection-status'),
        progressContainer: document.getElementById('progress-container'),
        btnRefreshArtifacts: document.getElementById('btn-refresh-artifacts'),
        artifactList: document.getElementById('artifact-list'),
        
        // Predictive Sizing & Preflight Anchors
        sizeProjection: document.getElementById('size-projection'),
        projectionValue: document.getElementById('projection-value'),
        preflightWarning: document.getElementById('preflight-warning'),
        badgeContainer: document.getElementById('badge-container'),
        
        // Modal Subsystem Anchors
        readmeModal: document.getElementById('readme-modal'),
        modalTitle: document.getElementById('modal-title'),
        modalBody: document.getElementById('modal-body'),
        btnCloseModal: document.getElementById('btn-close-modal')
    };

    // --- Initialization Phase ---
    if (authToken) authenticate(authToken);
    fetchArtifacts();

    // --- Keyboard Ergonomics ---
    dom.searchInput.addEventListener('keypress', (e) => { if (e.key === 'Enter' && !dom.btnSearch.disabled) dom.btnSearch.click(); });

    // --- Modal Subsystem Logic ---
    dom.btnCloseModal.addEventListener('click', closeReadmeModal);
    dom.readmeModal.addEventListener('click', (e) => {
        if (e.target === dom.readmeModal) closeReadmeModal();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && dom.readmeModal.style.display === 'flex') {
            closeReadmeModal();
        }
    });

    function closeReadmeModal() {
        // Strip focus from the close button before hiding the parent layer to prevent W3C ARIA violations
        if (document.activeElement instanceof HTMLElement) {
            document.activeElement.blur();
        }
        dom.readmeModal.style.display = 'none';
        dom.modalBody.innerHTML = '';
        dom.readmeModal.setAttribute('aria-hidden', 'true');
        dom.readmeModal.inert = true;
    }

    async function openReadmeModal(repoId) {
        dom.modalTitle.textContent = repoId;
        dom.modalBody.innerHTML = '<div class="text-secondary placeholder-text">Fetching repository manifest...</div>';
        dom.readmeModal.inert = false;
        dom.readmeModal.style.display = 'flex';
        dom.readmeModal.setAttribute('aria-hidden', 'false');
        
        try {
            // Direct CORS-friendly fetch to HuggingFace raw files
            const response = await fetch(`https://huggingface.co/${repoId}/raw/main/README.md`);
            if (!response.ok) {
                if (response.status === 404) throw new Error('README.md manifest not found in this repository.');
                throw new Error(`Upstream connection failed (${response.status})`);
            }
            
            const markdownText = await response.text();
            
            // Markdown parsing
            const rawHtml = marked.parse(markdownText);
            
            // AST Interception for safe anchor mutation
            DOMPurify.addHook('afterSanitizeAttributes', function(node) {
                if ('target' in node) {
                    node.setAttribute('target', '_blank');
                    node.setAttribute('rel', 'noopener noreferrer');
                }
            });
            
            // Execute sanitization with active hook
            const safeHtml = DOMPurify.sanitize(rawHtml);
            
            // Hermetic state reset
            DOMPurify.removeHook('afterSanitizeAttributes'); 
            
            dom.modalBody.innerHTML = safeHtml;

        } catch (error) {
            dom.modalBody.innerHTML = `<div class="error placeholder-text">[SYSTEM ERROR] ${error.message}</div>`;
        }
    }

    // --- Utility Functions ---
    function formatBytes(bytes, decimals = 2) {
        if (!+bytes) return '0 Bytes';
        const k = 1024;
        const dm = decimals < 0 ? 0 : decimals;
        const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return `${parseFloat((bytes / Math.pow(k, i)).toFixed(dm))} ${sizes[i]}`;
    }

    // --- Authentication Phase ---
    dom.authForm.addEventListener('submit', async (e) => {
        e.preventDefault(); // Defends against browser page reload defaults
        const token = dom.tokenInput.value.trim();
        if (!token) return;
        await authenticate(token);
    });

    async function authenticate(token) {
        dom.btnAuth.disabled = true;
        dom.authStatus.textContent = 'Verifying...';
        dom.authStatus.className = 'status-indicator';

        try {
            const response = await fetch('/api/auth/token', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ token: token })
            });

            const result = await response.json();

            if (response.ok) {
                authToken = token;
                localStorage.setItem('hf_token', token);
                
                const safeUsername = DOMPurify.sanitize(result.username);
                dom.authStatus.innerHTML = `Authenticated: ${safeUsername} <a href="#" id="logout-link" style="color: var(--error-color); margin-left: 10px; text-decoration: none;">[Clear]</a>`;
                dom.authStatus.className = 'status-indicator success';
                dom.btnSearch.disabled = false;
                
                dom.tokenInput.parentElement.style.display = 'none';
                dom.btnAuth.style.display = 'none';

                document.getElementById('logout-link').addEventListener('click', (e) => {
                    e.preventDefault();
                    clearAuthentication();
                });
            } else {
                throw new Error(result.detail || 'Authentication failed');
            }
        } catch (error) {
            dom.authStatus.textContent = error.message;
            dom.authStatus.className = 'status-indicator error';
            clearAuthentication(false);
        } finally {
            if (!authToken) dom.btnAuth.disabled = false;
        }
    }

    function clearAuthentication(clearStatusText = true) {
        authToken = null;
        localStorage.removeItem('hf_token');
        
        dom.tokenInput.value = '';
        dom.tokenInput.parentElement.style.display = 'flex';
        dom.btnAuth.style.display = 'inline-flex';
        dom.btnAuth.disabled = false;
        dom.btnSearch.disabled = true;
        
        if (clearStatusText) {
            dom.authStatus.textContent = '';
            dom.authStatus.className = 'status-indicator';
        }
    }

    // --- Hub Query & Pagination Phase ---
    dom.btnSearch.addEventListener('click', async () => {
        const query = dom.searchInput.value.trim();
        if (!query || !authToken) return;

        const sortParam = dom.filterSort.value;

        dom.btnSearch.disabled = true;
        dom.searchResults.innerHTML = '<div class="text-secondary placeholder-text">Querying repository metadata...</div>';
        dom.paginationModule.style.display = 'none';
        selectedRepoId = null;
        dom.btnExecute.disabled = true;
        currentSearchData = [];
        currentPage = 1;
        
        dom.sizeProjection.style.display = 'none';
        dom.preflightWarning.style.display = 'none';

        try {
            const response = await fetch(`/api/models/search?q=${encodeURIComponent(query)}&sort=${sortParam}`, {
                headers: { 'token': authToken }
            });
            
            const result = await response.json();

            if (response.ok) {
                currentSearchData = result.data;
                if (currentSearchData.length === 0) {
                    dom.searchResults.innerHTML = '<div class="text-secondary placeholder-text">No compatible repositories found.</div>';
                } else {
                    renderPage();
                }
            } else {
                throw new Error(result.detail || 'Search failed');
            }
        } catch (error) {
            dom.searchResults.textContent = `Error: ${error.message}`;
            dom.searchResults.className = 'error placeholder-text';
        } finally {
            dom.btnSearch.disabled = false;
        }
    });

    function renderPage() {
        const totalPages = Math.ceil(currentSearchData.length / itemsPerPage);
        const startIndex = (currentPage - 1) * itemsPerPage;
        const endIndex = startIndex + itemsPerPage;
        const pageData = currentSearchData.slice(startIndex, endIndex);

        renderSearchResults(pageData);

        if (totalPages > 1) {
            dom.paginationModule.style.display = 'flex';
            dom.pageIndicator.textContent = `Page ${currentPage} of ${totalPages}`;
            dom.btnPrevPage.disabled = currentPage === 1;
            dom.btnNextPage.disabled = currentPage === totalPages;
        } else {
            dom.paginationModule.style.display = 'none';
        }
    }

    dom.btnPrevPage.addEventListener('click', () => { if (currentPage > 1) { currentPage--; renderPage(); } });
    dom.btnNextPage.addEventListener('click', () => { const totalPages = Math.ceil(currentSearchData.length / itemsPerPage); if (currentPage < totalPages) { currentPage++; renderPage(); } });

    // --- Pre-Flight Heuristics Evaluation ---
    function evaluatePreFlight() {
        if (!selectedRepoId) {
            dom.sizeProjection.style.display = 'none';
            dom.preflightWarning.style.display = 'none';
            return;
        }
        
        // Regex extraction for parameter size in repo string (e.g., mixtral-8x7b, gemma-2-27b)
        const moeMatch = selectedRepoId.match(/(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)b/i);
        const denseMatch = selectedRepoId.match(/(?<!x)(\d+(?:\.\d+)?)b/i);

        let paramsBillions = 0;
        if (moeMatch) {
            paramsBillions = parseFloat(moeMatch[1]) * parseFloat(moeMatch[2]);
        } else if (denseMatch) {
            paramsBillions = parseFloat(denseMatch[1]);
        }

        if (paramsBillions > 0) {
            const selectedOption = dom.quantSelect.options[dom.quantSelect.selectedIndex];
            const bpw = parseFloat(selectedOption.getAttribute('data-bpw'));
            
            // Mathematically predict uncompressed VRAM footprint based on target precision
            const estimatedGb = (paramsBillions * 1e9 * bpw) / 8 / (1024 ** 3);
            
            // Expose the raw mathematical projection to the UI
            dom.projectionValue.textContent = estimatedGb.toFixed(2) + ' GB';
            dom.sizeProjection.style.display = 'block';
            
            if (estimatedGb >= 24.0) {
                dom.preflightWarning.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px;vertical-align:-2px;"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg> <b>Memory Warning:</b> Output artifact projected at ~${estimatedGb.toFixed(1)}GB. Ensure host system possesses adequate RAM/VRAM before initializing.`;
                dom.preflightWarning.style.display = 'block';
            } else {
                dom.preflightWarning.style.display = 'none';
            }
        } else {
            dom.sizeProjection.style.display = 'none';
            dom.preflightWarning.style.display = 'none';
        }
    }

    dom.quantSelect.addEventListener('change', evaluatePreFlight);

    function renderSearchResults(models) {
        dom.searchResults.innerHTML = '';

        models.forEach(model => {
            const card = document.createElement('div');
            card.className = 'model-card';
            if (model.repo_id === selectedRepoId) card.classList.add('selected');
            
            const safeRepoId = DOMPurify.sanitize(model.repo_id);
            const safeDownloads = DOMPurify.sanitize(model.downloads.toLocaleString());
            const safeLikes = DOMPurify.sanitize(model.likes.toLocaleString());
            
            card.innerHTML = `
                <div style="flex: 1; min-width: 0;">
                    <div class="model-header-row">
                        <div style="font-weight: 600; font-family: var(--font-mono); font-size: 0.875rem; word-break: break-all; padding-right: 0.5rem;">${safeRepoId}</div>
                        <button class="btn-info" aria-label="View README" title="View README manifest">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width: 1rem; height: 1rem;"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>
                        </button>
                    </div>
                    <div style="font-size: 0.75rem; color: var(--text-secondary); display: flex; gap: 1rem; margin-top: 0.25rem;">
                        <span>&darr; ${safeDownloads} Downloads</span>
                        <span>&hearts; ${safeLikes} Likes</span>
                    </div>
                </div>
            `;
            
            // Isolate info button click from card selection logic
            const btnInfo = card.querySelector('.btn-info');
            btnInfo.addEventListener('click', (e) => {
                e.stopPropagation();
                openReadmeModal(model.repo_id);
            });

            card.addEventListener('click', () => {
                document.querySelectorAll('.model-card').forEach(c => c.classList.remove('selected'));
                card.classList.add('selected');
                selectedRepoId = model.repo_id; 
                dom.btnExecute.disabled = false;
                evaluatePreFlight();
            });

            dom.searchResults.appendChild(card);
        });
    }

    // --- Pipeline Execution Phase ---
    dom.btnExecute.addEventListener('click', async () => {
        if (!selectedRepoId || !authToken) return;

        const quantProfile = dom.quantSelect.value;
        dom.btnExecute.disabled = true;
        dom.progressContainer.style.visibility = 'visible';
        dom.terminalOutput.textContent = 'Initializing runtime environment...\n';
        dom.badgeContainer.innerHTML = ''; // Reset badges

        try {
            const response = await fetch('/api/pipeline/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    token: authToken,
                    repo_id: selectedRepoId,
                    quant_profile: quantProfile
                })
            });

            const result = await response.json();

            if (response.ok) {
                initTelemetryStream(result.task_id);
            } else {
                throw new Error(result.detail || 'Pipeline initialization failed');
            }
        } catch (error) {
            appendTerminal(`[SYSTEM ERROR] ${error.message}\n`);
            dom.btnExecute.disabled = false;
            dom.progressContainer.style.visibility = 'hidden';
        }
    });

    // --- Dynamic Telemetry & UI Tagging ---
    let terminalBuffer = "";
    let terminalRafId = null;
    const trackedBadges = new Set();

    function addTelemetryBadge(label, color, bgColor) {
        if (trackedBadges.has(label)) return; // Prevent duplication
        trackedBadges.add(label);
        
        const badge = document.createElement('div');
        badge.className = 'arch-badge';
        badge.style.color = color;
        badge.style.backgroundColor = bgColor;
        badge.style.borderColor = color;
        badge.textContent = label;
        
        dom.badgeContainer.appendChild(badge);
    }

    function appendTerminal(text) {
        terminalBuffer += text;
        
        // Regex Telemetry Interception
        if (text.includes('High-Density/MoE Topology')) addTelemetryBadge('MoE Topology', '#7c3aed', '#ede9fe');
        if (text.includes('Injecting --jinja template')) addTelemetryBadge('Jinja Native', '#059669', '#d1fae5');
        if (text.includes('Safetensors verified')) addTelemetryBadge('Safetensors', '#2563eb', '#dbeafe');
        
        if (!terminalRafId) {
            terminalRafId = requestAnimationFrame(() => {
                dom.terminalOutput.textContent += terminalBuffer;
                terminalBuffer = "";
                
                const windowDiv = dom.terminalOutput.parentElement;
                windowDiv.scrollTop = windowDiv.scrollHeight;
                terminalRafId = null;
            });
        }
    }

    function initTelemetryStream(taskId) {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws/pipeline/${taskId}`;
        const ws = new WebSocket(wsUrl);

        ws.onopen = () => {
            dom.connectionStatus.textContent = 'Socket Active';
            dom.connectionStatus.className = 'status-indicator connected';
            trackedBadges.clear();
            fetchArtifacts(); 
        };

        ws.onmessage = (event) => { appendTerminal(event.data + '\n'); };

        ws.onerror = () => {
            appendTerminal('[SYSTEM ERROR] WebSocket connection anomaly detected.\n');
            dom.progressContainer.style.visibility = 'hidden';
        };

        ws.onclose = () => {
            dom.connectionStatus.textContent = 'Socket Closed';
            dom.connectionStatus.className = 'status-indicator disconnected';
            dom.btnExecute.disabled = false;
            dom.progressContainer.style.visibility = 'hidden';
            appendTerminal('[SYSTEM] Task finalized.\n');
            fetchArtifacts(); 
        };
    }

    // --- Artifact Repository Subsystem ---
    dom.btnRefreshArtifacts.addEventListener('click', fetchArtifacts);

    async function fetchArtifacts() {
        dom.artifactList.innerHTML = '<div class="text-secondary placeholder-text">Indexing local artifacts...</div>';
        try {
            const response = await fetch('/api/artifacts');
            const result = await response.json();
            
            if (response.ok) {
                renderArtifacts(result.data);
            } else {
                throw new Error(result.detail || 'Failed to index artifacts');
            }
        } catch (error) {
            dom.artifactList.textContent = `[SYSTEM ERROR] ${error.message}`;
            dom.artifactList.className = 'error placeholder-text';
        }
    }

    function renderArtifacts(artifacts) {
        dom.artifactList.innerHTML = '';
        if (artifacts.length === 0) {
            dom.artifactList.innerHTML = '<div class="text-secondary placeholder-text">No localized tensor artifacts found in output directory.</div>';
            return;
        }

        artifacts.forEach(artifact => {
            const safeName = DOMPurify.sanitize(artifact.filename);
            const isProcessing = safeName.endsWith('.processing');
            const displayName = isProcessing ? safeName.replace('.processing', '') : safeName;
            
            const formattedSize = isProcessing ? '--' : formatBytes(artifact.size_bytes);
            const formattedDate = new Date(artifact.created_at * 1000).toLocaleString();

            const card = document.createElement('div');
            card.className = 'artifact-card';
            
            let actionsHtml = isProcessing 
                ? `<div style="display: flex; align-items: center; gap: 0.5rem; color: var(--accent-color); font-size: 0.8125rem; font-weight: 600;">
                       <svg class="spin-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width: 1.125rem; height: 1.125rem;"><line x1="12" y1="2" x2="12" y2="6"></line><line x1="12" y1="18" x2="12" y2="22"></line><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"></line><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"></line><line x1="2" y1="12" x2="6" y2="12"></line><line x1="18" y1="12" x2="22" y2="12"></line><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"></line><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"></line></svg>
                       <span>Encoding...</span>
                   </div>`
                : `<button class="btn-small btn-outline bubbular btn-download" data-file="${safeName}">Download</button>
                   <button class="btn-small btn-danger bubbular btn-delete" data-file="${safeName}">Delete</button>`;

            card.innerHTML = `
                <div class="artifact-info" style="max-width: 60%;">
                    <span class="artifact-name" title="${displayName}">${displayName}</span>
                    <span class="artifact-meta">${formattedSize} | ${DOMPurify.sanitize(formattedDate)}</span>
                </div>
                <div class="artifact-actions">
                    ${actionsHtml}
                </div>
            `;

            if (!isProcessing) {
                card.querySelector('.btn-download').addEventListener('click', () => { window.location.href = `/api/artifacts/download/${encodeURIComponent(artifact.filename)}`; });
                const btnDelete = card.querySelector('.btn-delete');
                btnDelete.addEventListener('click', async () => {
                    if (confirm(`Execute destructive purge of ${safeName}? This action is irreversible.`)) {
                        btnDelete.disabled = true; btnDelete.textContent = 'Purging...';
                        try {
                            const response = await fetch(`/api/artifacts/${encodeURIComponent(artifact.filename)}`, { method: 'DELETE' });
                            if (response.ok) fetchArtifacts(); 
                            else throw new Error((await response.json()).detail || 'Purge failed');
                        } catch (error) {
                            alert(`[SYSTEM ERROR] ${error.message}`);
                            btnDelete.disabled = false; btnDelete.textContent = 'Delete';
                        }
                    }
                });
            }
            dom.artifactList.appendChild(card);
        });
    }
});