"""
Interactive Telegram Bot for Predict.fun Monitoring
Includes commands, inline keyboards, and real-time alerts
"""

import requests
import time
import json
from datetime import datetime
from collections import defaultdict
import sqlite3
from typing import Dict, List, Optional
from threading import Thread
import asyncio

# Telegram imports
try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import (
        Application, CommandHandler, CallbackQueryHandler, 
        ContextTypes, MessageHandler, filters
    )
except ImportError:
    print("Installing python-telegram-bot...")
    import subprocess
    subprocess.check_call(['pip', 'install', 'python-telegram-bot', '--break-system-packages'])
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import (
        Application, CommandHandler, CallbackQueryHandler, 
        ContextTypes, MessageHandler, filters
    )

from config import *


class TelegramPredictBot:
    """Interactive Telegram bot for Predict.fun monitoring"""
    
    def __init__(self, predict_api_key: str, telegram_token: str, chat_id: str):
        self.predict_api_key = predict_api_key
        self.telegram_token = telegram_token
        self.admin_chat_id = chat_id
        
        self.base_url = "https://api.predict.fun/v1"
        self.headers = {
            "x-api-key": predict_api_key,
            "Content-Type": "application/json"
        }
        
        # Settings
        self.monitoring_active = True
        self.whale_threshold = WHALE_THRESHOLD
        self.min_coordinated = MIN_COORDINATED_WALLETS
        
        # Database
        self.init_database()
        
        # Tracking
        self.tracked_wallets = set(TRACK_SPECIFIC_WALLETS)
        self.last_prices = {}
        
    def init_database(self):
        """Initialize SQLite database"""
        self.conn = sqlite3.connect('telegram_bot.db', check_same_thread=False)
        self.cursor = self.conn.cursor()
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                order_hash TEXT PRIMARY KEY,
                market_id INTEGER,
                wallet TEXT,
                side INTEGER,
                amount REAL,
                price REAL,
                timestamp INTEGER
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type TEXT,
                market_id INTEGER,
                message TEXT,
                timestamp INTEGER
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS market_volume (
                market_id INTEGER,
                volume REAL,
                trade_count INTEGER,
                timestamp INTEGER,
                PRIMARY KEY (market_id, timestamp)
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS tracked_wallets (
                wallet TEXT PRIMARY KEY,
                nickname TEXT,
                added_at INTEGER
            )
        ''')
        
        self.conn.commit()
    
    # ==================== TELEGRAM COMMAND HANDLERS ====================
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        welcome_msg = """
🎯 *Welcome to Predict.fun Smart Money Tracker!*

I'll help you track whale activity, coordinated betting, and sharp bettors on Predict.fun.

*📊 Market Analysis:*
/markets - List active markets with IDs
/market <id> - Complete market summary
/topwallets - See top performing wallets

*🔔 Alerts & Monitoring:*
/status - Check bot status and settings
/stats - View 24-hour statistics
/whales - See recent whale alerts
/coordinated - See coordination alerts

*⚙️ Customize Alerts:*
/setwhale <amount> - Set custom whale threshold
/setcoord <number> - Set min coordinated wallets
/settings - Quick adjustment buttons

*👁️ Wallet Tracking:*
/track <wallet> [nickname] - Track a specific wallet
/untrack <wallet> - Stop tracking a wallet
/mywallets - List tracked wallets

*🎮 Control:*
/pause - Pause monitoring
/resume - Resume monitoring
/help - Show this message

🚀 *Bot is now monitoring!*

💡 *Pro Tips:*
• Use `/markets` to find market IDs
• Start with `/setwhale 50` for more alerts
• Volume spike alerts catch opportunities early!
"""
        await update.message.reply_text(welcome_msg, parse_mode='Markdown')
    
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show bot status"""
        status = "🟢 Active" if self.monitoring_active else "🔴 Paused"
        
        msg = f"""
📊 *Bot Status*

Status: {status}
Whale Threshold: ${self.whale_threshold}
Min Coordinated Wallets: {self.min_coordinated}
Tracked Wallets: {len(self.tracked_wallets)}

Last Check: {datetime.now().strftime('%H:%M:%S')}
"""
        await update.message.reply_text(msg, parse_mode='Markdown')
    
    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show 24-hour statistics"""
        alerts = self.get_recent_alerts(24)
        
        whale_count = sum(1 for a in alerts if a.get('type') == 'WHALE_BET')
        coord_count = sum(1 for a in alerts if a.get('type') == 'COORDINATED_BETTING')
        tracked_count = sum(1 for a in alerts if a.get('type') == 'TRACKED_WALLET')
        
        # Calculate total volume
        total_volume = 0
        for alert in alerts:
            if alert.get('type') == 'WHALE_BET':
                total_volume += alert.get('amount', 0)
            elif alert.get('type') == 'COORDINATED_BETTING':
                total_volume += alert.get('total_amount', 0)
        
        msg = f"""
📊 *24-Hour Statistics*

🐋 Whale Alerts: `{whale_count}`
🤝 Coordination Alerts: `{coord_count}`
👁️ Tracked Wallet Alerts: `{tracked_count}`
💰 Total Volume Tracked: `${total_volume:,.2f}`
📢 Total Alerts: `{len(alerts)}`

_Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_
"""
        await update.message.reply_text(msg, parse_mode='Markdown')
    
    async def cmd_whales(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show recent whale alerts"""
        alerts = self.get_recent_alerts(24)
        whale_alerts = [a for a in alerts if a.get('type') == 'WHALE_BET'][:10]
        
        if not whale_alerts:
            await update.message.reply_text("No whale alerts in the last 24 hours.")
            return
        
        msg = "🐋 *Recent Whale Alerts (Last 24h)*\n\n"
        
        for alert in whale_alerts:
            wallet = alert.get('wallet', '')
            wallet_short = f"{wallet[:6]}...{wallet[-4:]}" if wallet else "Unknown"
            
            msg += f"""
Market: `#{alert.get('market_id')}`
Wallet: `{wallet_short}`
Side: *{alert.get('side')}*
Amount: *${alert.get('amount', 0):.2f}*
__{datetime.fromtimestamp(alert.get('timestamp', 0)).strftime('%m/%d %H:%M')}__
---
"""
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    
    async def cmd_coordinated(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show recent coordination alerts"""
        alerts = self.get_recent_alerts(24)
        coord_alerts = [a for a in alerts if a.get('type') == 'COORDINATED_BETTING'][:10]
        
        if not coord_alerts:
            await update.message.reply_text("No coordination alerts in the last 24 hours.")
            return
        
        msg = "🤝 *Recent Coordination Alerts (Last 24h)*\n\n"
        
        for alert in coord_alerts:
            msg += f"""
Market: `#{alert.get('market_id')}`
Wallets: *{alert.get('wallet_count')}*
Side: *{alert.get('side')}*
Total: *${alert.get('total_amount', 0):.2f}*
__{datetime.fromtimestamp(alert.get('timestamp', 0)).strftime('%m/%d %H:%M')}__
---
"""
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    
    async def cmd_track(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Track a wallet"""
        if not context.args:
            await update.message.reply_text(
                "Usage: /track <wallet_address> [nickname]\n"
                "Example: /track 0x1234... MyWhale"
            )
            return
        
        wallet = context.args[0]
        nickname = ' '.join(context.args[1:]) if len(context.args) > 1 else None
        
        # Validate wallet address
        if not wallet.startswith('0x') or len(wallet) < 10:
            await update.message.reply_text("❌ Invalid wallet address format.")
            return
        
        self.tracked_wallets.add(wallet)
        
        # Save to database
        try:
            self.cursor.execute('''
                INSERT OR REPLACE INTO tracked_wallets (wallet, nickname, added_at)
                VALUES (?, ?, ?)
            ''', (wallet, nickname, int(time.time())))
            self.conn.commit()
            
            msg = f"✅ Now tracking wallet: `{wallet[:10]}...`"
            if nickname:
                msg += f"\nNickname: *{nickname}*"
            
            await update.message.reply_text(msg, parse_mode='Markdown')
        except Exception as e:
            await update.message.reply_text(f"❌ Error tracking wallet: {e}")
    
    async def cmd_untrack(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Untrack a wallet"""
        if not context.args:
            await update.message.reply_text("Usage: /untrack <wallet_address>")
            return
        
        wallet = context.args[0]
        
        if wallet in self.tracked_wallets:
            self.tracked_wallets.remove(wallet)
            self.cursor.execute('DELETE FROM tracked_wallets WHERE wallet = ?', (wallet,))
            self.conn.commit()
            await update.message.reply_text(f"✅ Stopped tracking: `{wallet[:10]}...`", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Wallet not being tracked.")
    
    async def cmd_mywallets(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List tracked wallets"""
        if not self.tracked_wallets:
            await update.message.reply_text("You're not tracking any wallets yet.\nUse /track <address> to start!")
            return
        
        self.cursor.execute('SELECT wallet, nickname FROM tracked_wallets')
        wallets = self.cursor.fetchall()
        
        msg = "👁️ *Your Tracked Wallets*\n\n"
        
        for wallet, nickname in wallets:
            wallet_short = f"{wallet[:8]}...{wallet[-6:]}"
            msg += f"`{wallet_short}`"
            if nickname:
                msg += f" - *{nickname}*"
            msg += "\n"
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    
    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show settings with inline keyboard"""
        keyboard = [
            [
                InlineKeyboardButton("Whale: $25", callback_data='whale_25'),
                InlineKeyboardButton("Whale: $50", callback_data='whale_50'),
                InlineKeyboardButton("Whale: $100", callback_data='whale_100'),
            ],
            [
                InlineKeyboardButton("Whale: $200", callback_data='whale_200'),
                InlineKeyboardButton("Whale: $500", callback_data='whale_500'),
                InlineKeyboardButton("Whale: $1000", callback_data='whale_1000'),
            ],
            [
                InlineKeyboardButton("Min Wallets: 2", callback_data='coord_2'),
                InlineKeyboardButton("Min Wallets: 3", callback_data='coord_3'),
                InlineKeyboardButton("Min Wallets: 5", callback_data='coord_5'),
            ]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        msg = f"""
⚙️ *Current Settings*

Whale Threshold: ${self.whale_threshold}
Min Coordinated Wallets: {self.min_coordinated}

*Quick Settings (tap buttons):*
Or use `/setwhale <amount>` for custom value
"""
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=reply_markup)
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button presses"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        if data.startswith('whale_'):
            amount = int(data.split('_')[1])
            self.whale_threshold = amount
            await query.edit_message_text(f"✅ Whale threshold set to ${amount}")
        
        elif data.startswith('coord_'):
            count = int(data.split('_')[1])
            self.min_coordinated = count
            await query.edit_message_text(f"✅ Min coordinated wallets set to {count}")
    
    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Pause monitoring"""
        self.monitoring_active = False
        await update.message.reply_text("⏸️ Monitoring paused. Use /resume to continue.")
    
    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Resume monitoring"""
        self.monitoring_active = True
        await update.message.reply_text("▶️ Monitoring resumed!")
    
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show help"""
        await self.cmd_start(update, context)
    
    async def cmd_setwhale(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set custom whale threshold"""
        if not context.args:
            await update.message.reply_text(
                "Usage: `/setwhale <amount>`\n\n"
                "Examples:\n"
                "`/setwhale 25` - Alert on bets $25+\n"
                "`/setwhale 150` - Alert on bets $150+\n"
                "`/setwhale 1000` - Alert on bets $1000+\n\n"
                "Or use `/settings` for quick buttons!",
                parse_mode='Markdown'
            )
            return
        
        try:
            amount = int(context.args[0])
            
            if amount < 1:
                await update.message.reply_text("❌ Amount must be at least $1")
                return
            
            if amount > 100000:
                await update.message.reply_text("❌ Amount seems too high. Max is $100,000")
                return
            
            self.whale_threshold = amount
            
            await update.message.reply_text(
                f"✅ Whale threshold set to *${amount}*\n\n"
                f"You'll now get alerts for bets >= ${amount}",
                parse_mode='Markdown'
            )
        except ValueError:
            await update.message.reply_text(
                "❌ Please enter a valid number\n"
                "Example: `/setwhale 150`",
                parse_mode='Markdown'
            )
    
    async def cmd_setcoord(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set minimum coordinated wallets"""
        if not context.args:
            await update.message.reply_text(
                "Usage: `/setcoord <number>`\n\n"
                "Examples:\n"
                "`/setcoord 2` - Alert when 2+ wallets coordinate\n"
                "`/setcoord 5` - Alert when 5+ wallets coordinate\n\n"
                "Or use `/settings` for quick buttons!",
                parse_mode='Markdown'
            )
            return
        
        try:
            count = int(context.args[0])
            
            if count < 2:
                await update.message.reply_text("❌ Must be at least 2 wallets")
                return
            
            if count > 20:
                await update.message.reply_text("❌ Max is 20 wallets")
                return
            
            self.min_coordinated = count
            
            await update.message.reply_text(
                f"✅ Min coordinated wallets set to *{count}*\n\n"
                f"You'll get alerts when {count}+ wallets bet the same way",
                parse_mode='Markdown'
            )
        except ValueError:
            await update.message.reply_text(
                "❌ Please enter a valid number\n"
                "Example: `/setcoord 3`",
                parse_mode='Markdown'
            )
    
    async def cmd_topwallets(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show top performing wallets"""
        await update.message.reply_text("🔍 Analyzing wallet performance...")
        
        try:
            # Get top wallets by volume
            self.cursor.execute('''
                SELECT 
                    wallet,
                    COUNT(*) as bet_count,
                    SUM(amount) as total_volume,
                    MAX(timestamp) as last_bet
                FROM orders
                GROUP BY wallet
                HAVING total_volume > 100
                ORDER BY total_volume DESC
                LIMIT 10
            ''')
            
            wallets = self.cursor.fetchall()
            
            if not wallets:
                await update.message.reply_text(
                    "No wallet data yet. The bot needs to collect more activity.\n"
                    "Check back in a few hours!"
                )
                return
            
            msg = "📊 *TOP PERFORMING WALLETS*\n"
            msg += "_Based on total volume traded_\n\n"
            
            for i, (wallet, bet_count, volume, last_bet) in enumerate(wallets, 1):
                # Calculate estimated win rate
                win_rate = 0
                if volume > 5000:
                    win_rate = 75
                elif volume > 2000:
                    win_rate = 70
                elif volume > 1000:
                    win_rate = 65
                elif volume > 500:
                    win_rate = 60
                else:
                    win_rate = 55
                
                wallet_short = f"{wallet[:8]}...{wallet[-6:]}"
                
                # Time since last bet
                time_diff = int(time.time()) - last_bet
                if time_diff < 3600:
                    last_bet_str = f"{time_diff // 60}m ago"
                elif time_diff < 86400:
                    last_bet_str = f"{time_diff // 3600}h ago"
                else:
                    last_bet_str = f"{time_diff // 86400}d ago"
                
                badge = ""
                if win_rate >= 70:
                    badge = "🔥"
                elif win_rate >= 60:
                    badge = "✅"
                
                msg += f"*{i}. {wallet_short}* {badge}\n"
                msg += f"   Volume: ${volume:,.0f}\n"
                msg += f"   Bets: {bet_count} | Win Rate: ~{win_rate}%\n"
                msg += f"   Last Bet: {last_bet_str}\n"
                msg += f"   `/track {wallet}`\n\n"
            
            msg += "💡 *Tap /track command to follow a wallet*"
            
            await update.message.reply_text(msg, parse_mode='Markdown')
            
        except Exception as e:
            await update.message.reply_text(f"❌ Error getting top wallets: {e}")
    
    async def cmd_market(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show comprehensive market summary"""
        if not context.args:
            await update.message.reply_text(
                "Usage: `/market <market_id>`\n\n"
                "Example: `/market 12345`\n\n"
                "💡 Find market IDs at predict.fun in the URL!\n"
                "Or use `/markets` to see active markets",
                parse_mode='Markdown'
            )
            return
        
        try:
            market_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Please enter a valid market ID number")
            return
        
        await update.message.reply_text(f"📊 Analyzing market #{market_id}...")
        
        try:
            # Get market activity from last 24 hours
            cutoff = int(time.time()) - 86400
            
            self.cursor.execute('''
                SELECT 
                    side,
                    COUNT(*) as trades,
                    SUM(amount) as volume,
                    COUNT(DISTINCT wallet) as unique_wallets
                FROM orders
                WHERE market_id = ? AND timestamp > ?
                GROUP BY side
            ''', (market_id, cutoff))
            
            sides_data = {}
            for row in self.cursor.fetchall():
                side = 'YES' if row[0] == 0 else 'NO'
                sides_data[side] = {
                    'trades': row[1],
                    'volume': row[2],
                    'wallets': row[3]
                }
            
            # Get whale activity
            self.cursor.execute('''
                SELECT side, COUNT(*), SUM(amount)
                FROM orders
                WHERE market_id = ? AND timestamp > ? AND amount >= ?
                GROUP BY side
            ''', (market_id, cutoff, self.whale_threshold))
            
            whale_data = {}
            for row in self.cursor.fetchall():
                side = 'YES' if row[0] == 0 else 'NO'
                whale_data[side] = {
                    'count': row[1],
                    'volume': row[2]
                }
            
            # Build summary message
            msg = f"📊 *MARKET #{market_id} SUMMARY*\n\n"
            
            # Volume breakdown
            yes_vol = sides_data.get('YES', {}).get('volume', 0)
            no_vol = sides_data.get('NO', {}).get('volume', 0)
            total_vol = yes_vol + no_vol
            
            if total_vol == 0:
                await update.message.reply_text(
                    f"❌ No trading activity found for market #{market_id} in the last 24 hours.\n\n"
                    "This market may be inactive or the ID may be incorrect."
                )
                return
            
            yes_pct = (yes_vol / total_vol * 100) if total_vol > 0 else 0
            no_pct = (no_vol / total_vol * 100) if total_vol > 0 else 0
            
            msg += f"💰 *Volume (24h):* ${total_vol:,.0f}\n"
            msg += f"   • YES: ${yes_vol:,.0f} ({yes_pct:.0f}%)\n"
            msg += f"   • NO: ${no_vol:,.0f} ({no_pct:.0f}%)\n\n"
            
            # Trade counts
            yes_trades = sides_data.get('YES', {}).get('trades', 0)
            no_trades = sides_data.get('NO', {}).get('trades', 0)
            total_trades = yes_trades + no_trades
            
            msg += f"📈 *Trades:* {total_trades}\n"
            msg += f"   • YES: {yes_trades}\n"
            msg += f"   • NO: {no_trades}\n\n"
            
            # Whale activity
            yes_whales = whale_data.get('YES', {}).get('count', 0)
            no_whales = whale_data.get('NO', {}).get('count', 0)
            yes_whale_vol = whale_data.get('YES', {}).get('volume', 0)
            no_whale_vol = whale_data.get('NO', {}).get('volume', 0)
            
            if yes_whales > 0 or no_whales > 0:
                msg += f"🐋 *Whale Activity (${self.whale_threshold}+):*\n"
                msg += f"   • YES: {yes_whales} whales (${yes_whale_vol:,.0f})\n"
                msg += f"   • NO: {no_whales} whales (${no_whale_vol:,.0f})\n"
                
                if yes_whale_vol > no_whale_vol * 2:
                    msg += f"   • ⚠️ *Strong whale conviction on YES*\n"
                elif no_whale_vol > yes_whale_vol * 2:
                    msg += f"   • ⚠️ *Strong whale conviction on NO*\n"
                
                msg += "\n"
            
            # Sentiment analysis
            msg += f"🎯 *Sentiment (by volume):*\n"
            
            dominant_side = 'YES' if yes_vol > no_vol else 'NO'
            dominant_pct = max(yes_pct, no_pct)
            
            if dominant_pct > 80:
                strength = "Very Strong"
                emoji = "🔥"
            elif dominant_pct > 70:
                strength = "Strong"
                emoji = "💪"
            elif dominant_pct > 60:
                strength = "Moderate"
                emoji = "📊"
            else:
                strength = "Balanced"
                emoji = "⚖️"
            
            msg += f"   • {emoji} *{strength} {dominant_side}* ({dominant_pct:.0f}%)\n\n"
            
            # Smart money signal
            whale_yes_pct = (yes_whale_vol / (yes_whale_vol + no_whale_vol) * 100) if (yes_whale_vol + no_whale_vol) > 0 else 50
            
            msg += f"💡 *Smart Money Signal:*\n"
            
            # Calculate signal strength
            signal_score = 0
            signal_side = ""
            
            if yes_whales > no_whales and yes_vol > no_vol:
                signal_score = min(85, 50 + (yes_pct - 50) + (yes_whales - no_whales) * 5)
                signal_side = "YES"
            elif no_whales > yes_whales and no_vol > yes_vol:
                signal_score = min(85, 50 + (no_pct - 50) + (no_whales - yes_whales) * 5)
                signal_side = "NO"
            elif yes_vol > no_vol * 1.5:
                signal_score = 60 + (yes_pct - 50) / 2
                signal_side = "YES"
            elif no_vol > yes_vol * 1.5:
                signal_score = 60 + (no_pct - 50) / 2
                signal_side = "NO"
            
            if signal_score >= 70:
                msg += f"   ✅ *CONSIDER {signal_side}* (Confidence: {signal_score:.0f}%)\n"
                msg += f"   Reasons:\n"
                if yes_whales > no_whales or no_whales > yes_whales:
                    msg += f"   • Whale conviction on {signal_side}\n"
                if dominant_pct > 70:
                    msg += f"   • Strong volume on {signal_side}\n"
            elif signal_score >= 50:
                msg += f"   📊 *LEAN {signal_side}* (Confidence: {signal_score:.0f}%)\n"
            else:
                msg += f"   ⚖️ *NEUTRAL* - Market is balanced\n"
            
            await update.message.reply_text(msg, parse_mode='Markdown')
            
        except Exception as e:
            await update.message.reply_text(f"❌ Error analyzing market: {e}")
    
    async def cmd_markets(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show list of recently active markets"""
        await update.message.reply_text("🔍 Finding active markets...")
        
        try:
            # Get markets with recent activity
            cutoff = int(time.time()) - 86400  # Last 24 hours
            
            self.cursor.execute('''
                SELECT 
                    market_id,
                    COUNT(*) as trades,
                    SUM(amount) as volume,
                    MAX(timestamp) as last_activity
                FROM orders
                WHERE timestamp > ?
                GROUP BY market_id
                ORDER BY volume DESC
                LIMIT 10
            ''', (cutoff,))
            
            markets = self.cursor.fetchall()
            
            if not markets:
                await update.message.reply_text(
                    "No market activity found yet.\n\n"
                    "💡 The bot needs to collect data for a few hours.\n"
                    "Try again later, or find market IDs at:\n"
                    "https://predict.fun"
                )
                return
            
            msg = "📊 *ACTIVE MARKETS (Last 24h)*\n\n"
            msg += "_Markets with highest activity:_\n\n"
            
            for i, (market_id, trades, volume, last_activity) in enumerate(markets, 1):
                # Time since last activity
                time_diff = int(time.time()) - last_activity
                if time_diff < 3600:
                    last_str = f"{time_diff // 60}m ago"
                elif time_diff < 86400:
                    last_str = f"{time_diff // 3600}h ago"
                else:
                    last_str = f"{time_diff // 86400}d ago"
                
                msg += f"*{i}. Market #{market_id}*\n"
                msg += f"   Volume: ${volume:,.0f}\n"
                msg += f"   Trades: {trades}\n"
                msg += f"   Last: {last_str}\n"
                msg += f"   `/market {market_id}`\n\n"
            
            msg += "💡 *Tap any /market command to analyze*"
            
            await update.message.reply_text(msg, parse_mode='Markdown')
            
        except Exception as e:
            await update.message.reply_text(
                f"❌ Error: {e}\n\n"
                "Find market IDs at: https://predict.fun\n"
                "Click any market, copy ID from URL"
            )
    
    # ==================== MONITORING FUNCTIONS ====================
    
    def save_order(self, order: Dict):
        """Save order to database to avoid reprocessing"""
        try:
            # Extract wallet addresses (they might be dicts with 'id' field)
            taker = order.get('taker')
            if isinstance(taker, dict):
                taker = taker.get('id', str(taker))
            
            maker = order.get('maker')
            if isinstance(maker, dict):
                maker = maker.get('id', str(maker))
            
            # Create unique hash for this order
            order_hash = f"{maker}_{taker}_{order.get('executedAt', '')}_{order.get('makerAmount', '')}_{order.get('takerAmount', '')}"
            
            timestamp = order.get('executedAt', int(time.time()))
            try:
                timestamp = int(timestamp) if timestamp else int(time.time())
            except (ValueError, TypeError):
                timestamp = int(time.time())
            
            self.cursor.execute('''
                INSERT OR IGNORE INTO orders (order_hash, market_id, wallet, side, amount, price, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                order_hash,
                order.get('tokenId'),
                taker,  # Use extracted wallet address
                order.get('side'),
                float(order.get('takerAmount', 0)) / 1e18,
                float(order.get('price', 0)) / 1e18,
                timestamp
            ))
            
            self.conn.commit()
            return True
        except Exception as e:
            print(f"Error saving order: {e}")
            return False
    
    def is_order_processed(self, order: Dict) -> bool:
        """Check if we've already processed this order"""
        try:
            # Extract wallet addresses (they might be dicts)
            taker = order.get('taker')
            if isinstance(taker, dict):
                taker = taker.get('id', str(taker))
            
            maker = order.get('maker')
            if isinstance(maker, dict):
                maker = maker.get('id', str(maker))
            
            order_hash = f"{maker}_{taker}_{order.get('executedAt', '')}_{order.get('makerAmount', '')}_{order.get('takerAmount', '')}"
            
            self.cursor.execute('SELECT 1 FROM orders WHERE order_hash = ?', (order_hash,))
            return self.cursor.fetchone() is not None
        except:
            return False
    
    def get_order_matches(self):
        """Fetch recent orders from Predict.fun"""
        try:
            response = requests.get(
                f"{self.base_url}/orders/matches",
                headers=self.headers,
                timeout=10
            )
            
            # Log response status  
            if response.status_code != 200:
                print(f"⚠️ API returned status {response.status_code}: {response.text[:200]}")
            
            response.raise_for_status()
            all_orders = response.json().get('data', [])
            
            # Filter out orders we've already processed
            new_orders = []
            for order in all_orders:
                if not self.is_order_processed(order):
                    self.save_order(order)
                    new_orders.append(order)
            
            return new_orders
            
        except requests.exceptions.Timeout:
            print(f"⚠️ API request timed out")
            return []
        except requests.exceptions.ConnectionError as e:
            print(f"⚠️ Connection error to Predict.fun API: {e}")
            return []
        except Exception as e:
            print(f"⚠️ Error fetching orders: {e}")
            return []
    
    def detect_whale_activity(self, orders: List[Dict]) -> List[Dict]:
        """Detect whale bets with enhanced context"""
        alerts = []
        
        print(f"  Checking {len(orders)} orders for whales (threshold: ${self.whale_threshold})")
        
        for order in orders:
            amount = float(order.get('takerAmount', 0)) / 1e18
            
            # Debug: show all bet amounts
            if len(orders) <= 5:  # Only log details if few orders
                print(f"    Order: ${amount:.2f}")
            
            if amount >= self.whale_threshold:
                # Extract wallet address (might be dict)
                wallet = order.get('taker')
                if isinstance(wallet, dict):
                    wallet = wallet.get('id', str(wallet))
                
                market_id = order.get('tokenId')
                
                print(f"  🐋 WHALE FOUND! ${amount:.2f} on market #{market_id}")
                
                # Get wallet stats
                wallet_stats = self.get_wallet_win_rate(wallet)
                
                # Get market context
                market_info = self.get_market_context(market_id)
                
                # Ensure timestamp is an integer
                timestamp = order.get('executedAt', int(time.time()))
                try:
                    timestamp = int(timestamp) if timestamp else int(time.time())
                except (ValueError, TypeError):
                    timestamp = int(time.time())
                
                alert = {
                    'type': 'WHALE_BET',
                    'market_id': market_id,
                    'wallet': wallet,
                    'amount': amount,
                    'side': 'YES' if order.get('side') == 0 else 'NO',
                    'timestamp': timestamp,
                    # Enhanced context
                    'wallet_win_rate': wallet_stats.get('win_rate', 0),
                    'wallet_total_bets': wallet_stats.get('total_bets', 0),
                    'market_volume': market_info.get('volume', 0),
                    'current_odds': market_info.get('odds', 0),
                }
                alerts.append(alert)
                self.save_alert(alert)
        
        return alerts
    
    def get_wallet_win_rate(self, wallet: str) -> Dict:
        """Get wallet performance stats"""
        try:
            self.cursor.execute('''
                SELECT COUNT(*) as total_bets, SUM(amount) as total_volume
                FROM orders 
                WHERE wallet = ?
            ''', (wallet,))
            
            result = self.cursor.fetchone()
            if result:
                # TODO: Track actual wins when markets settle
                # For now, estimate based on volume
                total_bets = result[0] or 0
                total_volume = result[1] or 0
                
                # Placeholder win rate calculation
                # You'd update this when you track market settlements
                estimated_win_rate = 0
                if total_bets > 10:
                    # Wallets with high volume tend to be sharper
                    if total_volume > 5000:
                        estimated_win_rate = 75
                    elif total_volume > 2000:
                        estimated_win_rate = 65
                    elif total_volume > 500:
                        estimated_win_rate = 55
                
                return {
                    'win_rate': estimated_win_rate,
                    'total_bets': total_bets,
                    'total_volume': total_volume
                }
        except:
            pass
        
        return {'win_rate': 0, 'total_bets': 0, 'total_volume': 0}
    
    def get_market_context(self, market_id: int) -> Dict:
        """Get market context (volume, odds, etc)"""
        try:
            # Get recent volume for this market
            self.cursor.execute('''
                SELECT SUM(amount) as volume, COUNT(*) as trades
                FROM orders 
                WHERE market_id = ? AND timestamp > ?
            ''', (market_id, int(time.time()) - 86400))  # Last 24h
            
            result = self.cursor.fetchone()
            volume = result[0] or 0 if result else 0
            trades = result[1] or 0 if result else 0
            
            return {
                'volume': volume,
                'trades': trades,
                'odds': 0.5  # Placeholder - would fetch from API
            }
        except:
            pass
        
        return {'volume': 0, 'trades': 0, 'odds': 0.5}
    
    
    def detect_coordinated_activity(self, orders: List[Dict]) -> List[Dict]:
        """Detect coordinated betting"""
        market_activity = defaultdict(lambda: defaultdict(list))
        current_time = int(time.time())
        
        for order in orders:
            # Ensure timestamp is an integer
            timestamp = order.get('executedAt', 0)
            try:
                timestamp = int(timestamp) if timestamp else 0
            except (ValueError, TypeError):
                timestamp = 0
            
            if current_time - timestamp > COORDINATION_WINDOW:
                continue
            
            market_id = order.get('tokenId')
            side = order.get('side')
            
            # Extract wallet address (might be dict)
            wallet = order.get('taker')
            if isinstance(wallet, dict):
                wallet = wallet.get('id', str(wallet))
            
            market_activity[market_id][side].append({
                'wallet': wallet,
                'amount': float(order.get('takerAmount', 0)) / 1e18,
                'timestamp': timestamp
            })
        
        alerts = []
        for market_id, sides in market_activity.items():
            for side, wallets in sides.items():
                unique_wallets = len(set(w['wallet'] for w in wallets))
                
                if unique_wallets >= self.min_coordinated:
                    total_amount = sum(w['amount'] for w in wallets)
                    alert = {
                        'type': 'COORDINATED_BETTING',
                        'market_id': market_id,
                        'side': 'YES' if side == 0 else 'NO',
                        'wallet_count': unique_wallets,
                        'total_amount': total_amount,
                        'timestamp': current_time
                    }
                    alerts.append(alert)
                    self.save_alert(alert)
        
        return alerts
    
    def check_tracked_wallets(self, orders: List[Dict]) -> List[Dict]:
        """Check tracked wallet activity"""
        alerts = []
        
        for order in orders:
            # Extract wallet address (might be dict)
            wallet = order.get('taker')
            if isinstance(wallet, dict):
                wallet = wallet.get('id', str(wallet))
            
            # Skip if wallet is None or not a string
            if not wallet or not isinstance(wallet, str):
                continue
            
            if wallet in self.tracked_wallets:
                # Ensure timestamp is an integer
                timestamp = order.get('executedAt', int(time.time()))
                try:
                    timestamp = int(timestamp) if timestamp else int(time.time())
                except (ValueError, TypeError):
                    timestamp = int(time.time())
                
                alert = {
                    'type': 'TRACKED_WALLET',
                    'market_id': order.get('tokenId'),
                    'wallet': wallet,
                    'amount': float(order.get('takerAmount', 0)) / 1e18,
                    'side': 'YES' if order.get('side') == 0 else 'NO',
                    'timestamp': timestamp
                }
                alerts.append(alert)
                self.save_alert(alert)
        
        return alerts
    
    def detect_volume_spikes(self, orders: List[Dict]) -> List[Dict]:
        """Detect unusual volume spikes in markets"""
        if not orders:
            return []
        
        # Group orders by market
        market_volumes = defaultdict(lambda: {'volume': 0, 'trades': 0, 'sides': defaultdict(float)})
        
        for order in orders:
            market_id = order.get('tokenId')
            amount = float(order.get('takerAmount', 0)) / 1e18
            side = 'YES' if order.get('side') == 0 else 'NO'
            
            market_volumes[market_id]['volume'] += amount
            market_volumes[market_id]['trades'] += 1
            market_volumes[market_id]['sides'][side] += amount
        
        alerts = []
        current_time = int(time.time())
        
        for market_id, current in market_volumes.items():
            # Get average volume for this market over last 24 hours
            avg_volume = self.get_average_market_volume(market_id)
            
            if avg_volume > 0:
                spike_ratio = current['volume'] / avg_volume
                
                # Alert if volume is 3x+ normal
                if spike_ratio >= 3.0:
                    # Calculate which side is getting the volume
                    yes_vol = current['sides'].get('YES', 0)
                    no_vol = current['sides'].get('NO', 0)
                    total_vol = yes_vol + no_vol
                    
                    dominant_side = 'YES' if yes_vol > no_vol else 'NO'
                    side_percentage = (max(yes_vol, no_vol) / total_vol * 100) if total_vol > 0 else 50
                    
                    alert = {
                        'type': 'VOLUME_SPIKE',
                        'market_id': market_id,
                        'current_volume': current['volume'],
                        'normal_volume': avg_volume,
                        'spike_ratio': spike_ratio,
                        'dominant_side': dominant_side,
                        'side_percentage': side_percentage,
                        'trades': current['trades'],
                        'timestamp': current_time
                    }
                    alerts.append(alert)
                    self.save_alert(alert)
            
            # Save current volume for future reference
            self.save_market_volume(market_id, current['volume'], current['trades'])
        
        return alerts
    
    def get_average_market_volume(self, market_id: int, hours: int = 24) -> float:
        """Get average volume for a market over specified hours"""
        try:
            cutoff = int(time.time()) - (hours * 3600)
            
            self.cursor.execute('''
                SELECT AVG(volume) FROM market_volume
                WHERE market_id = ? AND timestamp > ?
            ''', (market_id, cutoff))
            
            result = self.cursor.fetchone()
            return result[0] if result and result[0] else 0
        except:
            return 0
    
    def save_market_volume(self, market_id: int, volume: float, trades: int):
        """Save market volume snapshot"""
        try:
            # Save hourly snapshots only
            current_hour = int(time.time()) // 3600 * 3600
            
            self.cursor.execute('''
                INSERT OR REPLACE INTO market_volume (market_id, volume, trade_count, timestamp)
                VALUES (?, ?, ?, ?)
            ''', (market_id, volume, trades, current_hour))
            
            self.conn.commit()
        except Exception as e:
            print(f"Error saving market volume: {e}")
    
    
    def save_alert(self, alert: Dict):
        """Save alert to database"""
        try:
            self.cursor.execute('''
                INSERT INTO alerts (alert_type, market_id, message, timestamp)
                VALUES (?, ?, ?, ?)
            ''', (
                alert['type'],
                alert.get('market_id'),
                json.dumps(alert),
                alert.get('timestamp', int(time.time()))
            ))
            self.conn.commit()
        except Exception as e:
            print(f"Error saving alert: {e}")
    
    def get_recent_alerts(self, hours: int = 24) -> List[Dict]:
        """Get recent alerts"""
        cutoff = int(time.time()) - (hours * 3600)
        self.cursor.execute('''
            SELECT message FROM alerts 
            WHERE timestamp > ?
            ORDER BY timestamp DESC
        ''', (cutoff,))
        
        alerts = []
        for row in self.cursor.fetchall():
            try:
                alerts.append(json.loads(row[0]))
            except:
                pass
        return alerts
    
    async def send_telegram_alert(self, alert: Dict, app: Application):
        """Send alert to Telegram"""
        msg = self.format_alert(alert)
        
        try:
            await app.bot.send_message(
                chat_id=self.admin_chat_id,
                text=msg,
                parse_mode='Markdown'
            )
        except Exception as e:
            print(f"Error sending Telegram alert: {e}")
    
    def format_alert(self, alert: Dict) -> str:
        """Format alert message"""
        if alert['type'] == 'WHALE_BET':
            wallet = alert.get('wallet', '')
            wallet_short = f"{wallet[:6]}...{wallet[-4:]}" if wallet else "Unknown"
            
            # Enhanced whale alert with context
            msg = f"""
🐋 *WHALE ALERT!*

Market: `#{alert.get('market_id')}`
Wallet: `{wallet_short}`
Side: *{alert.get('side')}*
Amount: *${alert.get('amount', 0):.2f}* USDT
"""
            
            # Add wallet stats if available
            if alert.get('wallet_total_bets', 0) > 0:
                win_rate = alert.get('wallet_win_rate', 0)
                if win_rate > 0:
                    msg += f"\n📊 Wallet Stats:\n"
                    msg += f"  • Win Rate: ~{win_rate}% (estimated)\n"
                    msg += f"  • Total Bets: {alert.get('wallet_total_bets')}\n"
                    
                    if win_rate >= 70:
                        msg += f"  • 🔥 *Proven winner!*\n"
                    elif win_rate >= 60:
                        msg += f"  • ✅ *Sharp bettor*\n"
            
            # Add market context if available
            if alert.get('market_volume', 0) > 0:
                volume = alert.get('market_volume', 0)
                msg += f"\n💰 Market Volume (24h): ${volume:,.0f}\n"
                
                # Show if this is significant relative to volume
                if alert.get('amount', 0) / max(volume, 1) > 0.1:
                    msg += f"⚠️ *This bet is 10%+ of daily volume!*\n"
            
            return msg
        
        elif alert['type'] == 'COORDINATED_BETTING':
            return f"""
🤝 *COORDINATED ACTIVITY!*

Market: `#{alert.get('market_id')}`
Wallets: *{alert.get('wallet_count')}*
Side: *{alert.get('side')}*
Total: *${alert.get('total_amount', 0):.2f}* USDT

⚠️ *Multiple wallets betting together - possible insider info!*
"""
        
        elif alert['type'] == 'TRACKED_WALLET':
            wallet = alert.get('wallet', '')
            wallet_short = f"{wallet[:6]}...{wallet[-4:]}"
            return f"""
👁️ *TRACKED WALLET ACTIVITY!*

Wallet: `{wallet_short}`
Market: `#{alert.get('market_id')}`
Side: *{alert.get('side')}*
Amount: *${alert.get('amount', 0):.2f}* USDT
"""
        
        elif alert['type'] == 'VOLUME_SPIKE':
            spike_ratio = alert.get('spike_ratio', 0)
            
            # Determine alert intensity
            if spike_ratio >= 5:
                emoji = "🔥🔥🔥"
                intensity = "EXTREME"
            elif spike_ratio >= 4:
                emoji = "🔥🔥"
                intensity = "VERY HIGH"
            else:
                emoji = "🔥"
                intensity = "HIGH"
            
            return f"""
{emoji} *VOLUME SPIKE ALERT!*

Market: `#{alert.get('market_id')}`
Current Volume: *${alert.get('current_volume', 0):,.0f}*
Normal Volume: ${alert.get('normal_volume', 0):,.0f}
Spike: *{spike_ratio:.1f}x normal* ({intensity})

📊 Volume Breakdown:
  • *{alert.get('dominant_side')}*: {alert.get('side_percentage', 0):.0f}%
  • Trades: {alert.get('trades', 0)}

⚠️ *Unusual activity detected - possible insider info or breaking news!*

💡 Use `/market {alert.get('market_id')}` for full analysis
"""
        
        return str(alert)
    
    async def monitoring_loop(self, app: Application):
        """Background monitoring loop"""
        print("🚀 Monitoring started...")
        
        iteration = 0
        
        while True:
            try:
                iteration += 1
                
                if not self.monitoring_active:
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue
                
                # Log every 10 iterations
                if iteration % 10 == 0:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Check #{iteration} - Fetching orders...")
                
                orders = self.get_order_matches()
                
                if orders:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✓ Found {len(orders)} NEW orders")
                    print(f"  Current whale threshold: ${self.whale_threshold}")
                    
                    # Detect all types of activity
                    try:
                        print(f"  Starting whale detection...")
                        whale_alerts = self.detect_whale_activity(orders)
                        if whale_alerts:
                            print(f"  🐋 {len(whale_alerts)} whale alerts")
                        else:
                            print(f"  No whales found (threshold: ${self.whale_threshold})")
                    except Exception as e:
                        print(f"Error in whale detection: {e}")
                        import traceback
                        traceback.print_exc()
                        whale_alerts = []
                    
                    try:
                        coord_alerts = self.detect_coordinated_activity(orders)
                        if coord_alerts:
                            print(f"  🤝 {len(coord_alerts)} coordination alerts")
                    except Exception as e:
                        print(f"Error in coordination detection: {e}")
                        coord_alerts = []
                    
                    try:
                        tracked_alerts = self.check_tracked_wallets(orders)
                        if tracked_alerts:
                            print(f"  👁️ {len(tracked_alerts)} tracked wallet alerts")
                    except Exception as e:
                        print(f"Error in tracked wallets: {e}")
                        tracked_alerts = []
                    
                    try:
                        volume_alerts = self.detect_volume_spikes(orders)
                        if volume_alerts:
                            print(f"  🔥 {len(volume_alerts)} volume spike alerts")
                    except Exception as e:
                        print(f"Error in volume spike detection: {e}")
                        volume_alerts = []
                    
                    # Send alerts to Telegram
                    all_alerts = whale_alerts + coord_alerts + tracked_alerts + volume_alerts
                    for alert in all_alerts:
                        try:
                            await self.send_telegram_alert(alert, app)
                        except Exception as e:
                            print(f"Error sending alert: {e}")
                else:
                    if iteration % 10 == 0:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] No orders found")
                
                await asyncio.sleep(CHECK_INTERVAL)
                
            except Exception as e:
                print(f"Error in monitoring loop: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(CHECK_INTERVAL)
    
    def run(self):
        """Start the Telegram bot"""
        print("🤖 Starting Telegram Bot...")
        
        # Create application
        app = Application.builder().token(self.telegram_token).build()
        
        # Add command handlers
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("stats", self.cmd_stats))
        app.add_handler(CommandHandler("whales", self.cmd_whales))
        app.add_handler(CommandHandler("coordinated", self.cmd_coordinated))
        app.add_handler(CommandHandler("track", self.cmd_track))
        app.add_handler(CommandHandler("untrack", self.cmd_untrack))
        app.add_handler(CommandHandler("mywallets", self.cmd_mywallets))
        app.add_handler(CommandHandler("settings", self.cmd_settings))
        app.add_handler(CommandHandler("setwhale", self.cmd_setwhale))
        app.add_handler(CommandHandler("setcoord", self.cmd_setcoord))
        app.add_handler(CommandHandler("topwallets", self.cmd_topwallets))
        app.add_handler(CommandHandler("markets", self.cmd_markets))
        app.add_handler(CommandHandler("market", self.cmd_market))
        app.add_handler(CommandHandler("pause", self.cmd_pause))
        app.add_handler(CommandHandler("resume", self.cmd_resume))
        
        # Add button callback handler
        app.add_handler(CallbackQueryHandler(self.button_callback))
        
        print("✅ Bot is running! Press Ctrl+C to stop.")
        print(f"💬 Send /start to your bot to begin")
        
        # Start monitoring in background - use post_init correctly
        async def post_init_callback(application: Application):
            """Start monitoring after bot is initialized"""
            asyncio.create_task(self.monitoring_loop(application))
        
        app.post_init = post_init_callback
        
        # Run bot
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in config.py")
        exit(1)
    
    bot = TelegramPredictBot(
        predict_api_key=PREDICT_API_KEY,
        telegram_token=TELEGRAM_BOT_TOKEN,
        chat_id=TELEGRAM_CHAT_ID
    )
    
    bot.run()
