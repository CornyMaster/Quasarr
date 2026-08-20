# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

from html import escape

import quasarr.providers.html_images as images
from quasarr.providers.html_templates import render_button, render_centered_html
from quasarr.providers.page_dispatch import render_page
from quasarr.providers.statistics import StatsHelper


def setup_statistics(app, shared_state):
    def _classic_statistics():
        stats_helper = StatsHelper(shared_state)
        stats = stats_helper.get_stats()
        cohort_state = escape(str(stats["crypter_sweep_state"]))
        individual_mode = escape(str(stats["crypter_individual_mode"] or "None"))

        stats_html = f"""
        <h1><img src="{images.logo}" type="image/webp" alt="Quasarr logo" class="logo"/>Quasarr</h1>
        <h2>Statistics</h2>
        <div class="stats-container">
            <h3>📊 Overview</h3>
            <div class="stats-grid compact">
                <div class="stat-card highlight">
                    <h3>📦 Total Download Attempts</h3>
                    <div class="stat-value">{stats["total_download_attempts"]:,}</div>
                    <div class="stat-subtitle">Success Rate: {stats["download_success_rate"]:,.1f}%</div>
                </div>
                <div class="stat-card highlight">
                    <h3>🔐 Total CAPTCHA Decryptions</h3>
                    <div class="stat-value">{stats["total_captcha_decryptions"]:,}</div>
                    <div class="stat-subtitle">Success Rate: {stats["decryption_success_rate"]:,.1f}%</div>
                </div>
            </div>

            <h3>⬇️ Downloads</h3>
            <div class="stats-grid compact">
                <div class="stat-card">
                    <h3>✅ Packages Downloaded</h3>
                    <div class="stat-value">{stats["packages_downloaded"]:,}</div>
                </div>
                <div class="stat-card">
                    <h3>⚙️ Links Processed</h3>
                    <div class="stat-value">{stats["links_processed"]:,}</div>
                </div>
                <div class="stat-card">
                    <h3>❌ Failed Downloads</h3>
                    <div class="stat-value">{stats["failed_downloads"]:,}</div>
                </div>
                <div class="stat-card">
                    <h3>🔗 Average Links per Package</h3>
                    <div class="stat-value">{stats["average_links_per_package"]:,.1f}</div>
                </div>
            </div>

            <h3>🧩 CAPTCHAs</h3>
            <div class="stats-grid compact">
                <div class="stat-card">
                    <h3>🤖 Automatic Decryptions</h3>
                    <div class="stat-value">{stats["captcha_decryptions_automatic"]:,}</div>
                    <div class="stat-subtitle">Success Rate: {stats["automatic_decryption_success_rate"]:,.1f}%</div>
                </div>
                <div class="stat-card">
                    <h3>👤 Manual Decryptions</h3>
                    <div class="stat-value">{stats["captcha_decryptions_manual"]:,}</div>
                    <div class="stat-subtitle">Success Rate: {stats["manual_decryption_success_rate"]:,.1f}%</div>
                </div>
                <div class="stat-card">
                    <h3>⛔ Failed Auto Decryptions</h3>
                    <div class="stat-value">{stats["failed_decryptions_automatic"]:,}</div>
                </div>
                <div class="stat-card">
                    <h3>🚫 Failed Manual Decryptions</h3>
                    <div class="stat-value">{stats["failed_decryptions_manual"]:,}</div>
                </div>
            </div>

            <h3>🔒 Linkcrypter Blocks</h3>
            <div class="stats-grid compact">
                <div class="stat-card">
                    <h3>👁️ Block Observations</h3>
                    <div class="stat-value">{stats["crypter_block_observations"]:,}</div>
                </div>
                <div class="stat-card">
                    <h3>⏳ Cooldowns Started</h3>
                    <div class="stat-value">{stats["crypter_cooldowns"]:,}</div>
                </div>
                <div class="stat-card">
                    <h3>🔁 Probes Spent</h3>
                    <div class="stat-value">{stats["crypter_probes"]:,}</div>
                </div>
                <div class="stat-card">
                    <h3>🕒 Deferred Packages</h3>
                    <div class="stat-value">{stats["deferred_packages"]:,}</div>
                    <div class="stat-subtitle">Currently waiting</div>
                </div>
            </div>

            <h3>Filecrypt cohort</h3>
            <div class="stats-grid compact">
                <div class="stat-card">
                    <h3>State</h3>
                    <div class="stat-value">{cohort_state}</div>
                </div>
                <div class="stat-card">
                    <h3>Tested</h3>
                    <div class="stat-value">{stats["crypter_sweep_tested"]:,} / {stats["crypter_sweep_total"]:,}</div>
                </div>
                <div class="stat-card">
                    <h3>Deadline</h3>
                    <div class="stat-value">{stats["crypter_sweep_deadline_epoch"]:,}</div>
                </div>
                <div class="stat-card">
                    <h3>Cooldowns</h3>
                    <div class="stat-value">{stats["crypter_cooldown_count"]:,}</div>
                </div>
                <div class="stat-card">
                    <h3>Retest queue</h3>
                    <div class="stat-value">{stats["crypter_retest_depth"]:,}</div>
                </div>
                <div class="stat-card">
                    <h3>Individual mode</h3>
                    <div class="stat-value">{individual_mode}</div>
                </div>
            </div>

            <h3>Terminal operations</h3>
            <div class="stats-grid compact">
                <div class="stat-card">
                    <h3>Prepared</h3>
                    <div class="stat-value">{stats["terminal_operations_prepared"]:,}</div>
                </div>
                <div class="stat-card">
                    <h3>Submitted</h3>
                    <div class="stat-value">{stats["terminal_operations_submitted"]:,}</div>
                </div>
                <div class="stat-card">
                    <h3>Complete</h3>
                    <div class="stat-value">{stats["terminal_operations_complete"]:,}</div>
                </div>
            </div>

            <h3>🗂️ Cached Metadata</h3>
            <div class="stats-grid compact">
                <div class="stat-card">
                    <h3>💾 Total Cached Entries</h3>
                    <div class="stat-value">{stats["metadata_total_cached"]:,}</div>
                </div>
                <div class="stat-card">
                    <h3>🎬 IMDb Cached IDs</h3>
                    <div class="stat-value">{stats["imdb_total_cached"]:,}</div>
                </div>
                <div class="stat-card">
                    <h3>🏷️ IMDb With Title</h3>
                    <div class="stat-value">{stats["imdb_with_title"]:,}</div>
                </div>
                <div class="stat-card">
                    <h3>🖼️ IMDb With Poster</h3>
                    <div class="stat-value">{stats["imdb_with_poster"]:,}</div>
                </div>
                <div class="stat-card">
                    <h3>🌍 IMDb With Localized Title</h3>
                    <div class="stat-value">{stats["imdb_with_localized"]:,}</div>
                </div>
                <div class="stat-card">
                    <h3>🧩 XEM Global Name Index</h3>
                    <div class="stat-value">{stats["xem_all_names_valid"]:,}</div>
                    <div class="stat-subtitle">{stats["xem_all_names_cached"]:,} cached entry</div>
                </div>
                <div class="stat-card">
                    <h3>📺 XEM Season Name Caches</h3>
                    <div class="stat-value">{stats["xem_season_total_cached"]:,}</div>
                    <div class="stat-subtitle">{stats["xem_season_valid_cached"]:,} valid</div>
                </div>
            </div>
        </div>

        <p>
            {render_button("Back", "secondary", {"onclick": "location.href='/'"})}
        </p>

        <style>
            .stats-container {{
                max-width: 1000px;
                margin: 0 auto;
            }}

            .stats-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                gap: 15px;
                margin: 15px 0;
            }}

            .stats-grid.compact {{
                gap: 12px;
                margin: 12px 0;
            }}

            .stat-card {{
                background: var(--card-bg, #f8f9fa);
                border: 1px solid var(--card-border, #dee2e6);
                border-radius: 8px;
                padding: 15px;
                text-align: center;
                transition: transform 0.2s, box-shadow 0.2s;
            }}

            .stat-card:hover {{
                transform: translateY(-2px);
                box-shadow: 0 4px 12px var(--card-shadow, rgba(0,0,0,0.1));
            }}

            .stat-card.highlight {{
                background: var(--highlight-bg, #e3f2fd);
                border-color: var(--highlight-border, #2196f3);
            }}

            .stat-card h3 {{
                margin: 0 0 8px 0;
                font-size: 13px;
                color: var(--text-muted, #666);
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }}

            .stat-value {{
                font-size: 24px;
                font-weight: bold;
                color: var(--text-primary, #333);
                margin: 8px 0;
            }}

            .stat-subtitle {{
                font-size: 11px;
                color: var(--text-secondary, #888);
                margin-top: 4px;
            }}

            h3 {{
                color: var(--heading-color, #444);
                padding-bottom: 8px;
                margin-top: 25px;
                margin-bottom: 15px;
            }}

            /* Dark mode styles */
            @media (prefers-color-scheme: dark) {{
                :root {{
                    --card-border: #4a5568;
                    --card-shadow: rgba(0,0,0,0.3);
                    --highlight-bg: #1a365d;
                    --highlight-border: #3182ce;
                    --text-muted: #a0aec0;
                    --text-primary: #f7fafc;
                    --text-secondary: #cbd5e0;
                    --heading-color: #e2e8f0;
                    --border-color: #4a5568;
                }}
            }}

            /* Force dark mode styles for applications that don't support prefers-color-scheme */
            body.dark-mode .stat-card {{
                background: #2d3748;
                border-color: #4a5568;
                color: #f7fafc;
            }}

            body.dark-mode .stat-card:hover {{
                box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            }}

            body.dark-mode .stat-card.highlight {{
                background: #1a365d;
                border-color: #3182ce;
            }}

            body.dark-mode .stat-card h3 {{
                color: #a0aec0;
            }}

            body.dark-mode .stat-value {{
                color: #f7fafc;
            }}

            body.dark-mode .stat-subtitle {{
                color: #cbd5e0;
            }}

            body.dark-mode h2 {{
                color: #e2e8f0;
                border-bottom-color: #4a5568;
            }}
        </style>
        """

        return render_centered_html(stats_html)

    @app.get("/statistics")
    def statistics():
        def carbon():
            from quasarr.api.statistics.carbon import render_statistics

            return render_statistics(shared_state)

        return render_page(
            "statistics",
            carbon,
            _classic_statistics,
            shared_state=shared_state,
        )
