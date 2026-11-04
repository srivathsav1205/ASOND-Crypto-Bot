let currentChartSymbol = "BINANCE:BTCUSDT";
let tvWidget = null;
let currentRsTab = "leaders";
let latestDashboardData = null;

function escapeHtml(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function renderRsList() {
  if (!latestDashboardData) return;
  const data = latestDashboardData;
  const rsList = document.getElementById('rs-list');
  if (!rsList) return;

  const rawItems = currentRsTab === 'major' 
    ? (data.major_relative_strength || []) 
    : (data.top_relative_strength || []);

  if (rawItems && rawItems.length > 0) {
    rsList.innerHTML = rawItems.map(item => {
      let sym = '', rs = 0, pxNum = null, chgNum = null;
      if (Array.isArray(item)) {
        sym = String(item[0] || '');
        rs = Number(item[1]);
        pxNum = item.length > 2 ? item[2] : null;
        chgNum = item.length > 3 ? item[3] : null;
      } else if (item && typeof item === 'object') {
        sym = String(item.symbol || item[0] || '');
        rs = Number(item.rs != null ? item.rs : item[1]);
        pxNum = item.price != null ? item.price : item[2];
        chgNum = item.change_24h != null ? item.change_24h : item[3];
      }

      if (!sym || isNaN(rs)) return '';

      let pxStr = '';
      if (pxNum != null && Number.isFinite(Number(pxNum))) {
        const n = Number(pxNum);
        pxStr = n >= 1 ? '$' + n.toFixed(2) : '$' + n.toPrecision(4);
      }
      const chgStr = chgNum != null && Number.isFinite(Number(chgNum)) ? `${Number(chgNum) >= 0 ? '+' : ''}${Number(chgNum).toFixed(2)}%` : '';
      const rsStr = `${rs >= 0 ? '+' : ''}${rs.toFixed(2)}% RS`;
      const rsClass = rs >= 0 ? 'rs-val-pos' : 'rs-val-neg';

      return `
        <div class="rs-item" data-chart-symbol="${escapeHtml(sym)}" style="cursor:pointer;" title="Click to view ${escapeHtml(sym)} chart">
          <div class="rs-left">
            <span class="rs-sym">${escapeHtml(sym)}</span>
            ${pxStr ? `<span class="rs-px">${pxStr}</span>` : ''}
          </div>
          <div class="rs-right">
            ${chgStr ? `<span class="rs-chg">${chgStr}</span>` : ''}
            <span class="${rsClass}">${rsStr}</span>
          </div>
        </div>
      `;
    }).filter(Boolean).join('');
  } else {
    rsList.innerHTML = '<div style="color:var(--text-muted); padding:10px; text-align:center; font-size:0.8rem;">No RS data available</div>';
  }
}

async function closePosition(symbol, evt) {
  if (evt) evt.stopPropagation();
  if (!confirm(`Are you sure you want to close ${symbol} at market?`)) return;
  try {
    const res = await fetch('/close-position', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Dashboard-Token': window.dashboardToken || ''
      },
      body: JSON.stringify({ symbol: symbol })
    });
    const result = await res.json();
    if (res.ok && result.ok) {
      alert(`Position ${symbol} closed successfully.`);
      updateDashboard();
    } else {
      alert(`Failed to close ${symbol}: ${result.error || 'Request failed'}`);
    }
  } catch (err) {
    alert(`Error closing position ${symbol}: ${err.message}`);
  }
}

function renderTradingViewChart(symbol) {
  currentChartSymbol = symbol.startsWith("BINANCE:") ? symbol : "BINANCE:" + symbol;
  const labelEl = document.getElementById('chart-curr-symbol');
  if (labelEl) labelEl.textContent = currentChartSymbol;

  // Highlight active quick button
  const cleanSym = currentChartSymbol.replace("BINANCE:", "").replace("USDT", "");
  document.querySelectorAll('.coin-btn').forEach(btn => {
    btn.classList.toggle('active', btn.textContent === cleanSym);
  });

  const container = document.getElementById('tv_chart_container');
  if (!container) return;
  container.innerHTML = '';

  if (typeof TradingView !== 'undefined' && TradingView.widget) {
    new TradingView.widget({
      "autosize": true,
      "symbol": currentChartSymbol,
      "interval": "60",
      "timezone": "Etc/UTC",
      "theme": "dark",
      "style": "1",
      "locale": "en",
      "toolbar_bg": "#111827",
      "enable_publishing": false,
      "hide_top_toolbar": false,
      "hide_legend": false,
      "save_image": false,
      "container_id": "tv_chart_container",
      "backgroundColor": "#111827",
      "gridColor": "rgba(255, 255, 255, 0.04)"
    });
  }
}

function switchChart(symbol) {
  renderTradingViewChart(symbol);
}

// Initialize chart on load
window.addEventListener('DOMContentLoaded', () => {
  renderTradingViewChart("BINANCE:BTCUSDT");
});

async function updateDashboard() {
  try {
    const res = await fetch('/status');
    if (!res.ok) return;
    const data = await res.json();
    if (data.dashboard_token) window.dashboardToken = data.dashboard_token;

    // 1. Mode Badge
    const badgeMode = document.getElementById('badge-mode');
    const modeText = document.getElementById('mode-text');
    if (badgeMode && modeText) {
      if (data.live_trading) {
        badgeMode.className = 'badge badge-red';
        modeText.textContent = 'LIVE ORDERS ACTIVE';
        const cfgMode = document.getElementById('cfg-mode');
        if (cfgMode) cfgMode.textContent = 'Live Trading';
      } else {
        badgeMode.className = 'badge badge-green';
        modeText.textContent = 'SIGNAL-ONLY (PAPER)';
        const cfgMode = document.getElementById('cfg-mode');
        if (cfgMode) cfgMode.textContent = 'Signal-Only (Paper)';
      }
    }

    // 2. BTC Macro Regime Badge
    const badgeBtc = document.getElementById('badge-btc');
    if (badgeBtc) {
      const btcStatus = data.btc_regime || 'Unknown';
      if (btcStatus.toLowerCase().includes('healthy')) {
        badgeBtc.className = 'badge badge-green';
        badgeBtc.innerHTML = '<span>🟢 ' + escapeHtml(btcStatus) + '</span>';
      } else {
        badgeBtc.className = 'badge badge-yellow';
        badgeBtc.innerHTML = '<span>⚠️ ' + escapeHtml(btcStatus) + '</span>';
      }
    }

    // 3. Strategy Capital & Available
    const valEquity = document.getElementById('val-equity');
    if (valEquity) {
      const equityVal = data.equity_usdt != null ? '$' + Number(data.equity_usdt).toFixed(2) : '$0.00';
      valEquity.textContent = equityVal;
    }
    const subFree = document.getElementById('sub-free');
    if (subFree) {
      const freeVal = data.free_usdt != null ? '$' + Number(data.free_usdt).toFixed(2) : '$0.00';
      subFree.textContent = 'Available: ' + freeVal;
    }

    // 4. Amount Invested (Real active capital)
    const valInvested = document.getElementById('val-invested');
    if (valInvested) {
      const invested = Number(data.amount_invested_usdt || 0);
      valInvested.textContent = '$' + invested.toFixed(2);
    }
    const subInvestedInfo = document.getElementById('sub-invested-info');
    if (subInvestedInfo) {
      const openPosCount = Object.keys(data.positions || {}).length;
      const maxPos = (data.risk && data.risk.max_positions) ? data.risk.max_positions : 5;
      subInvestedInfo.textContent = openPosCount + ' / ' + maxPos + ' Open Positions';
    }

    // 5. Win Rate % (Real closed trades)
    const winrate = Number(data.win_rate_pct || 0);
    const winrateEl = document.getElementById('val-winrate');
    if (winrateEl) {
      winrateEl.textContent = winrate.toFixed(1) + '%';
      if (winrate >= 50) {
        winrateEl.style.color = 'var(--accent-green)';
      } else if (data.total_closed_trades > 0) {
        winrateEl.style.color = 'var(--accent-yellow)';
      } else {
        winrateEl.style.color = 'var(--text-main)';
      }
    }
    const wins = data.wins_count || 0;
    const losses = data.losses_count || 0;
    const totalClosed = data.total_closed_trades || 0;
    const subWinrateInfo = document.getElementById('sub-winrate-info');
    if (subWinrateInfo) {
      subWinrateInfo.textContent = wins + ' Wins / ' + losses + ' Losses (' + totalClosed + ' Closed)';
    }

    // 6. Realized PnL
    const pnl = Number(data.realized_pnl_usdt || 0);
    const pnlEl = document.getElementById('val-realized-pnl');
    if (pnlEl) {
      pnlEl.textContent = (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2);
      pnlEl.style.color = pnl >= 0 ? (pnl > 0 ? 'var(--accent-green)' : 'var(--text-main)') : 'var(--accent-red)';
    }
    const subDayPct = document.getElementById('sub-day-pct');
    if (subDayPct) {
      subDayPct.textContent = totalClosed > 0 ? (wins + ' of ' + totalClosed + ' trades won') : 'No closed trades yet';
    }

    // 7. Universe & Config
    const valUniverse = document.getElementById('val-universe');
    if (valUniverse) {
      valUniverse.textContent = (data.eligible_symbol_count || data.symbol_count || 0) + ' pairs';
    }
    const cfgSize = document.getElementById('cfg-size');
    if (cfgSize && data.risk && data.risk.trade_notional_usdt) {
      cfgSize.textContent = '$' + Number(data.risk.trade_notional_usdt).toFixed(2) + ' Notional';
    }

    // 8. Relative Strength Leaderboard (Clickable to switch chart)
    latestDashboardData = data;
    const btcTextEl = document.getElementById('rs-benchmark-text');
    if (btcTextEl) {
      const btcChg = data.btc_24h_change_pct != null ? data.btc_24h_change_pct : null;
      if (btcChg != null && Number.isFinite(Number(btcChg))) {
        btcTextEl.innerHTML = `Benchmark: BTC 24h: <strong style="color:var(--text-main);">${Number(btcChg) >= 0 ? '+' : ''}${Number(btcChg).toFixed(2)}%</strong>`;
      }
    }
    const ageEl = document.getElementById('rs-age-text');
    if (ageEl) {
      const age = data.rs_age_seconds;
      if (age != null && Number.isFinite(Number(age))) {
        ageEl.textContent = age < 4 ? 'Synced just now' : `Synced ${Math.round(age)}s ago`;
      } else {
        ageEl.textContent = 'Live Feed';
      }
    }
    renderRsList();

    // 8b. Active Positions Table
    const positionsBody = document.getElementById('positions-body');
    if (positionsBody) {
      const posList = Array.isArray(data.positions) ? data.positions : Object.values(data.positions || {});
      const activePosCountEl = document.getElementById('active-pos-count');
      if (activePosCountEl) activePosCountEl.textContent = posList.length + ' active';
      if (posList.length > 0) {
        positionsBody.innerHTML = posList.map(p => {
          const sym = escapeHtml(p.symbol);
          const qty = Number(p.quantity || 0).toFixed(4);
          const entry = Number(p.entry_price || 0).toFixed(4);
          const mark = p.mark_price != null ? Number(p.mark_price).toFixed(4) : '--';
          const pnlVal = p.unrealized_pnl_usdt != null ? Number(p.unrealized_pnl_usdt) : null;
          const pnlStr = pnlVal != null ? ((pnlVal >= 0 ? '+$' : '-$') + Math.abs(pnlVal).toFixed(2)) : '--';
          const pnlStyle = pnlVal != null ? (pnlVal >= 0 ? 'style="color:var(--accent-green); font-weight:600;"' : 'style="color:var(--accent-red); font-weight:600;"') : '';
          return `
            <tr data-chart-symbol="${sym}" style="cursor:pointer;" title="Click to view ${sym} chart">
              <td><strong>${sym}</strong></td>
              <td>${qty}</td>
              <td>$${entry}</td>
              <td>$${mark}</td>
              <td ${pnlStyle}>${pnlStr}</td>
              <td><button class="close-pos-btn" data-close-symbol="${sym}" style="background:#ef4444; color:#fff; border:none; padding:4px 8px; border-radius:4px; font-size:0.72rem; cursor:pointer; font-weight:600;">Close</button></td>
            </tr>
          `;
        }).join('');
      } else {
        positionsBody.innerHTML = `<tr><td colspan="6" style="text-align:center; color:var(--text-muted); padding:12px;">No active positions</td></tr>`;
      }
    }

    // 9. Trades & Signals Table
    const tradesBody = document.getElementById('trades-body');
    if (tradesBody) {
      if (data.recent_trades && data.recent_trades.length > 0) {
        const tradeCountEl = document.getElementById('trade-count');
        if (tradeCountEl) tradeCountEl.textContent = data.recent_trades.length + ' recorded';
        tradesBody.innerHTML = data.recent_trades.slice(0, 10).map(t => {
          const timeStr = new Date(t.time * 1000).toLocaleTimeString();
          const score = t.score || 0;
          const scoreClass = score >= 80 ? 'score-high' : (score >= 65 ? 'score-mid' : 'score-low');
          let statusBadge = `<span style="color:var(--accent-blue);">${escapeHtml(t.status)}</span>`;
          if (t.status === 'OPEN') statusBadge = `<span style="color:var(--accent-green); font-weight:700;">🟢 OPEN</span>`;
          if (t.status === 'CLOSED_TARGET' || t.status === 'CLOSED_OCO_TARGET') statusBadge = `<span style="color:var(--accent-green); font-weight:700;">🎯 WIN (Target)</span>`;
          if (t.status === 'CLOSED_STOP' || t.status === 'CLOSED_OCO_STOP') statusBadge = `<span style="color:var(--accent-red); font-weight:700;">🛑 STOP</span>`;
          if (t.status === 'CLOSED_STOP_FALLBACK') statusBadge = `<span style="color:var(--accent-red); font-weight:700;">⚠️ STOP FALLBACK</span>`;
          if (t.status === 'CLOSED_TIME_STOP') statusBadge = `<span style="color:var(--accent-yellow); font-weight:700;">⏱️ TIME STOP</span>`;
          if (t.status === 'CLOSED_MANUAL_MARKET') statusBadge = `<span style="color:var(--accent-yellow); font-weight:700;">🛑 MANUAL EXIT</span>`;
          if (t.status === 'CLOSED' || t.status === 'CLOSED_OCO_EXIT') statusBadge = `<span style="color:var(--accent-blue); font-weight:700;">🏁 CLOSED</span>`;

          return `
            <tr data-chart-symbol="${escapeHtml(t.symbol)}" style="cursor:pointer;" title="Click to view ${escapeHtml(t.symbol)} chart">
              <td>${timeStr}</td>
              <td><strong>${escapeHtml(t.symbol)}</strong></td>
              <td>${statusBadge}</td>
              <td>$${Number(t.entry || 0).toFixed(4)}</td>
              <td>$${Number(t.stop || 0).toFixed(4)}</td>
              <td style="color:var(--accent-green); font-weight:600;">$${Number(t.target || 0).toFixed(4)}</td>
              <td><span class="score-pill ${scoreClass}">${score > 0 ? score + '/100' : 'N/A'}</span></td>
            </tr>
          `;
        }).join('');
      } else {
        const uCount = data.eligible_symbol_count || data.symbol_count || 0;
        const uText = uCount > 0 ? `Scanning ${uCount} pairs for breakouts...` : 'Scanning pairs for breakouts...';
        tradesBody.innerHTML = `<tr><td colspan="7" style="text-align:center; color:var(--text-muted); padding:16px;">${escapeHtml(uText)}</td></tr>`;
      }
    }

    // 10. Real-Time Event Feed
    const eventFeed = document.getElementById('event-feed');
    if (eventFeed && data.recent_events && data.recent_events.length > 0) {
      eventFeed.innerHTML = data.recent_events.slice(0, 25).map(ev => {
        const timeStr = new Date(ev.time * 1000).toLocaleTimeString();
        return `
          <div class="event-row">
            <span class="event-time">${timeStr}</span>
            <span class="event-msg">${escapeHtml(ev.message)}</span>
          </div>
        `;
      }).join('');
    }

  } catch (err) {
    console.error("Dashboard update failed:", err);
  }
}

// Event delegation: replace all inline onclick handlers with clean data-* attribute dispatch
document.addEventListener('click', function(e) {
  const tabLeaders = e.target.closest('#rs-tab-leaders');
  if (tabLeaders) {
    currentRsTab = "leaders";
    tabLeaders.classList.add('active');
    const tabMajor = document.getElementById('rs-tab-major');
    if (tabMajor) tabMajor.classList.remove('active');
    renderRsList();
    return;
  }
  const tabMajor = e.target.closest('#rs-tab-major');
  if (tabMajor) {
    currentRsTab = "major";
    tabMajor.classList.add('active');
    const tabLeaders = document.getElementById('rs-tab-leaders');
    if (tabLeaders) tabLeaders.classList.remove('active');
    renderRsList();
    return;
  }
  const closeBtn = e.target.closest('[data-close-symbol]');
  if (closeBtn) {
    e.stopPropagation();
    const sym = closeBtn.getAttribute('data-close-symbol');
    if (sym) closePosition(sym, e);
    return;
  }
  const chartTarget = e.target.closest('[data-chart-symbol]');
  if (chartTarget) {
    const sym = chartTarget.getAttribute('data-chart-symbol');
    if (sym) switchChart(sym);
    return;
  }
  const stopBtn = e.target.closest('#btn-stop-bot');
  if (stopBtn) {
    e.stopPropagation();
    stopBotCleanly();
    return;
  }
});

async function stopBotCleanly() {
  const btn = document.getElementById('btn-stop-bot');
  if (!confirm('Are you sure you want to stop the bot cleanly?')) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Stopping...';
  }
  try {
    const res = await fetch('/stop-bot', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Dashboard-Token': window.dashboardToken || ''
      },
      body: JSON.stringify({ reason: 'user_dashboard_stop' })
    });
    const result = await res.json().catch(() => ({ ok: true }));
    if (btn) btn.textContent = '⏹ Stopped';
    alert('Bot daemon has been cleanly stopped.');
  } catch (err) {
    if (btn) btn.textContent = '⏹ Stopped';
    alert('Bot daemon stopped.');
  }
}

setInterval(updateDashboard, 2000);
updateDashboard();
