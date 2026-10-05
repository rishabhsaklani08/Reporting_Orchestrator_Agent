document.addEventListener('DOMContentLoaded', () => {
    // UI Elements
    const searchInput = document.getElementById('search-input');
    const searchBtn = document.getElementById('search-btn');
    const emptyState = document.getElementById('empty-state');
    const loadingState = document.getElementById('loading-state');
    const resultsList = document.getElementById('results-list');

    // Sidebar Elements
    const catalogCountEl = document.getElementById('catalog-count');
    const llmStatusEl = document.getElementById('llm-status');
    const syncBtn = document.getElementById('sync-btn');
    const toast = document.getElementById('toast');

    // Settings
    const llmSlider = document.getElementById('llm-top-k');
    const llmVal = document.getElementById('llm-val');

    // Selection Elements
    const selectedList = document.getElementById('selected-list');
    const selectedCount = document.getElementById('selected-count');
    const proceedBtn = document.getElementById('proceed-btn');
    const confirmOverlay = document.getElementById('confirm-overlay');
    const confirmMessage = document.getElementById('confirm-message');

    // State
    const selectedReports = new Set();

    // Sync overlay
    const syncOverlay = document.getElementById('sync-overlay');

    // Initialize Settings Labels
    llmSlider.addEventListener('input', (e) => llmVal.textContent = e.target.value);

    // Initial Load: Check sync status (which also fetches stats when done)
    checkSyncStatus();

    // Event Listeners
    searchBtn.addEventListener('click', handleSearch);
    searchInput.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') handleSearch();
    });
    syncBtn.addEventListener('click', handleSync);
    proceedBtn.addEventListener('click', handleProceed);

    async function checkSyncStatus() {
        try {
            const res = await fetch('api/sync-status');
            const data = await res.json();

            if (data.status === 'syncing') {
                // Show overlay and poll every 2 seconds
                syncOverlay.classList.remove('hidden');
                setTimeout(checkSyncStatus, 2000);
            } else {
                // Sync done, failed, or idle — hide overlay and load stats
                syncOverlay.classList.add('hidden');
                fetchStats();

                if (data.status === 'done') {
                    showToast(`Catalog updated: ${data.num_reports.toLocaleString()} reports loaded`, 'success');
                } else if (data.status === 'failed') {
                    showToast('Auto-sync failed — using existing catalog', 'error');
                }
            }
        } catch (error) {
            // Server probably not ready yet — hide overlay, try stats normally
            syncOverlay.classList.add('hidden');
            fetchStats();
        }
    }

    async function fetchStats() {
        try {
            const res = await fetch('api/stats');
            const data = await res.json();
            catalogCountEl.textContent = data.num_reports.toLocaleString();

            if (data.llm_enabled) {
                llmStatusEl.textContent = 'Active';
                llmStatusEl.classList.remove('offline');
            } else {
                llmStatusEl.textContent = 'Offline (Base Only)';
                llmStatusEl.classList.add('offline');
            }
        } catch (error) {
            console.error("Failed to fetch stats:", error);
            showToast("Failed to connect to backend", "error");
        }
    }

    async function handleSearch() {
        const query = searchInput.value.trim();
        if (!query) return;

        // Update UI State
        emptyState.classList.add('hidden');
        resultsList.innerHTML = '';
        loadingState.classList.remove('hidden');

        const requestBody = {
            query: query,
            bm25_top_n: 15, // Reduced to stay within model token limits
            llm_top_k: parseInt(llmSlider.value),
            use_llm: true
        };

        try {
            const res = await fetch('api/search', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(requestBody)
            });

            if (!res.ok) throw new Error("Search failed");

            const data = await res.json();
            renderResults(data.results);

            // Warn user if LLM fell back to BM25
            if (data.llm_fallback) {
                const reason = data.fallback_reason || "LLM unavailable";
                showToast(`⚠ ${reason} — results are ranked by keyword match only.`, "warning");
            }
        } catch (error) {
            console.error("Search Error:", error);
            showToast("Search failed to execute.", "error");
            emptyState.classList.remove('hidden');
        } finally {
            loadingState.classList.add('hidden');
        }
    }

    function renderResults(results) {
        if (!results || results.length === 0) {
            resultsList.innerHTML = `
                <div class="empty-state">
                    <div class="empty-icon">🤷</div>
                    <h2>No results found</h2>
                    <p>Try adjusting your query or settings.</p>
                </div>
            `;
            return;
        }

        resultsList.innerHTML = results.map((result, index) => {
            const report = result.report || {};
            const scoreStr = typeof result.score === 'number' ? result.score.toFixed(1) : result.score;
            const isSelected = selectedReports.has(result.report_name);

            return `
                <div class="result-card band-${result.band} ${isSelected ? 'selected' : ''}" style="animation: slideUp 0.3s ease ${index * 0.05}s both;">
                    <div class="card-header">
                        <div class="card-select">
                            <label class="checkbox-container">
                                <input type="checkbox" class="report-checkbox"
                                       data-name="${escapeHtml(result.report_name)}"
                                       ${isSelected ? 'checked' : ''}>
                                <span class="checkmark"></span>
                            </label>
                            <div class="card-title">${escapeHtml(result.report_name)}</div>
                        </div>
                        <div class="score-badge">
                            <span class="score-value">${scoreStr}%</span>
                            <span class="score-label">${result.band}</span>
                        </div>
                    </div>
                    
                    ${result.explanation ? `<div class="card-explanation"><strong>Why:</strong> ${escapeHtml(result.explanation)}</div>` : ''}
                    
                    <details class="card-details">
                        <summary>View Details</summary>
                        <div class="details-content">
                            <div class="meta-item"><strong>Report Name:</strong> <span>${escapeHtml(result.report_name || 'N/A')}</span></div>
                            <div class="meta-item"><strong>Report Type:</strong> <span>${escapeHtml(report.Report_Type || 'N/A')}</span></div>
                            <div class="meta-item"><strong>Description:</strong> <span>${escapeHtml(report.Brief_Description || 'N/A')}</span></div>
                            <div class="meta-item"><strong>Data Source:</strong> <span>${escapeHtml(report.DS_Description || 'N/A')}</span></div>
                            <div class="meta-item"><strong>Fields Displayed:</strong> <span>${escapeHtml(report.Fields_Displayed_on_Report || 'N/A')}</span></div>
                            <div class="meta-item"><strong>Fields Referenced:</strong> <span>${escapeHtml(report.Fields_Referenced_in_Report || 'N/A')}</span></div>
                        </div>
                    </details>
                </div>
            `;
        }).join('');

        // Attach checkbox listeners
        document.querySelectorAll('.report-checkbox').forEach(cb => {
            cb.addEventListener('change', (e) => {
                const name = e.target.dataset.name;
                const card = e.target.closest('.result-card');
                if (e.target.checked) {
                    selectedReports.add(name);
                    card.classList.add('selected');
                } else {
                    selectedReports.delete(name);
                    card.classList.remove('selected');
                }
                updateSelectedPanel();
            });
        });

        // Also allow clicking the card (not the details) to toggle
        document.querySelectorAll('.result-card').forEach(card => {
            card.addEventListener('click', (e) => {
                if (e.target.closest('details') || e.target.closest('.report-checkbox') ||
                    e.target.closest('.checkbox-container')) return;

                const cb = card.querySelector('.report-checkbox');
                cb.checked = !cb.checked;
                cb.dispatchEvent(new Event('change'));
            });
        });
    }

    function updateSelectedPanel() {
        const count = selectedReports.size;
        selectedCount.textContent = count;
        proceedBtn.disabled = count === 0;

        if (count === 0) {
            selectedList.innerHTML = '<p class="selected-empty">Search and select reports to migrate.</p>';
            return;
        }

        selectedList.innerHTML = Array.from(selectedReports).map(name => `
            <div class="selected-item">
                <span class="selected-name">${escapeHtml(name)}</span>
                <button class="remove-btn" data-name="${escapeHtml(name)}" title="Remove">×</button>
            </div>
        `).join('');

        selectedList.querySelectorAll('.remove-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const name = e.target.dataset.name;
                selectedReports.delete(name);
                const cb = document.querySelector(`.report-checkbox[data-name="${CSS.escape(name)}"]`);
                if (cb) {
                    cb.checked = false;
                    cb.closest('.result-card')?.classList.remove('selected');
                }
                updateSelectedPanel();
            });
        });
    }

    async function handleProceed() {
        if (selectedReports.size === 0) return;

        const reports = Array.from(selectedReports);
        proceedBtn.disabled = true;
        proceedBtn.innerHTML = '<div class="spinner" style="width:16px;height:16px;border-width:2px;margin:0 8px 0 0;"></div> Sending...';

        try {
            const res = await fetch('api/confirm', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ reports: reports })
            });

            if (!res.ok) throw new Error("Confirmation failed");

            const data = await res.json();
            confirmMessage.textContent = `${reports.length} report(s) selected: ${reports.join(', ')}`;
            confirmOverlay.classList.remove('hidden');
            showToast(data.message, "success");
        } catch (error) {
            console.error("Confirm error:", error);
            showToast("Failed to confirm selection.", "error");
            proceedBtn.disabled = false;
            proceedBtn.innerHTML = 'Proceed to Migration';
        }
    }

    function escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    async function handleSync() {
        const originalText = syncBtn.innerHTML;
        syncBtn.innerHTML = '<div class="spinner" style="width:16px;height:16px;border-width:2px;margin:0 8px 0 0;"></div> Syncing...';
        syncBtn.disabled = true;

        try {
            const res = await fetch('api/sync', { method: 'POST' });
            const data = await res.json();

            if (res.ok && data.success) {
                showToast(data.message, "success");
                fetchStats(); // Update count
            } else {
                throw new Error(data.detail || "Sync failed");
            }
        } catch (error) {
            console.error("Sync error:", error);
            showToast(error.message, "error");
        } finally {
            syncBtn.innerHTML = originalText;
            syncBtn.disabled = false;
        }
    }

    function showToast(message, type = "success") {
        toast.textContent = message;
        toast.className = `toast ${type}`;
        toast.classList.remove('hidden');

        const delay = type === 'warning' ? 6000 : 3000;
        setTimeout(() => {
            toast.style.transform = 'translateY(100px)';
            toast.style.opacity = '0';
            setTimeout(() => {
                toast.classList.add('hidden');
                toast.style.transform = '';
                toast.style.opacity = '';
            }, 300);
        }, delay);
    }
});

// Add keyframe animations dynamically
const style = document.createElement('style');
style.textContent = `
    @keyframes slideUp {
        from { opacity: 0; transform: translateY(20px); }
        to { opacity: 1; transform: translateY(0); }
    }
`;
document.head.appendChild(style);
