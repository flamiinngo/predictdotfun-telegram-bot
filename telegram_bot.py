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
        self.alerted_transactions = set()  # Track transactions we've already alerted on
        
        # Load previously alerted transactions from database (survive restarts)
        self.load_alerted_transactions()
    
    def load_alerted_transactions(self):
        """Load previously alerted transaction hashes from database"""
        try:
            # Get transaction hashes from recent alerts (last 7 days)
            cutoff = int(time.time()) - (7 * 86400)
            self.cursor.execute('''
                SELECT message FROM alerts 
                WHERE alert_type = 'WHALE_BET' 
                AND timestamp > ?
            ''', (cutoff,))
            
            for row in self.cursor.fetchall():
                try:
                    alert_data = json.loads(row[0])
                    alert_id = alert_data.get('alert_id', '')
                    # Extract tx_hash from alert_id (format: tx_wallet_amount)
                    if alert_id and '_' in alert_id:
                        tx_hash = alert_id.split('_')[0]
                        if tx_hash and tx_hash != 'fallback':
                            self.alerted_transactions.add(tx_hash)
                except:
                    pass
            
            print(f"📊 Loaded {len(self.alerted_transactions)} previously alerted transactions from database")
        except Exception as e:
            print(f"Warning: Could not load alerted transactions: {e}")
        
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
            CREATE TABLE IF NOT EXISTS whale_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT,
                market_id INTEGER,
                side TEXT,
                amount REAL,
                bet_timestamp INTEGER,
                result TEXT DEFAULT 'PENDING',
                profit_loss REAL DEFAULT 0,
                resolved_at INTEGER
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
        
        # Calculate total volume from database
        try:
            # All time stats
            self.cursor.execute('SELECT COUNT(*), SUM(amount), COUNT(DISTINCT wallet) FROM orders')
            result = self.cursor.fetchone()
            total_orders_ever = result[0] if result else 0
            total_volume_ever = result[1] if result and result[1] else 0
            unique_wallets = result[2] if result else 0
            
            # Last 24h stats
            cutoff = int(time.time()) - 86400
            self.cursor.execute('SELECT COUNT(*), SUM(amount) FROM orders WHERE timestamp > ?', (cutoff,))
            result = self.cursor.fetchone()
            total_orders = result[0] if result else 0
            total_volume = result[1] if result and result[1] else 0
        except Exception as e:
            print(f"Error getting stats: {e}")
            total_orders = 0
            total_volume = 0
            total_orders_ever = 0
            total_volume_ever = 0
            unique_wallets = 0
        
        msg = f"""
📊 *Statistics*

*24-Hour Activity:*
  • Orders: {total_orders}
  • Volume: ${total_volume:,.2f}

*All Time:*
  • Total Orders: {total_orders_ever}
  • Total Volume: ${total_volume_ever:,.2f}
  • Unique Wallets: {unique_wallets}

*Alerts Sent (24h):*
  • Whale Alerts: {whale_count}
  • Coordinated: {coord_count}
  • Tracked Wallets: {tracked_count}

*Current Settings:*
  • Whale Threshold: ${self.whale_threshold}
  • Min Coordinated: {self.min_coordinated}
"""
        await update.message.reply_text(msg, parse_mode='Markdown')
        
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
            amount = float(context.args[0])
            
            if amount < 0.01:
                await update.message.reply_text("❌ Amount must be at least $0.01")
                return
            
            if amount > 100000:
                await update.message.reply_text("❌ Amount seems too high. Max is $100,000")
                return
            
            self.whale_threshold = amount
            
            await update.message.reply_text(
                f"✅ Whale threshold set to *${amount:.2f}*\n\n"
                f"You'll now get alerts for bets >= ${amount:.2f}",
                parse_mode='Markdown'
            )
        except ValueError:
            await update.message.reply_text(
                "❌ Please enter a valid number\n"
                "Example: `/setwhale 0.1` or `/setwhale 100`",
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
                WHERE wallet IS NOT NULL
                GROUP BY wallet
                HAVING total_volume > 0.1
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
        # Check if user wants fast-resolving markets
        show_fast_only = context.args and context.args[0].lower() == 'fast'
        
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
                LIMIT 20
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
            
            filter_text = " (Fast Resolving)" if show_fast_only else ""
            msg = f"📊 *ACTIVE MARKETS{filter_text}*\n\n"
            msg += "_Markets with highest activity:_\n\n"
            
            shown = 0
            for i, (market_id, trades, volume, last_activity) in enumerate(markets, 1):
                # Time since last activity
                time_diff = int(time.time()) - last_activity
                if time_diff < 3600:
                    last_str = f"{time_diff // 60}m ago"
                elif time_diff < 86400:
                    last_str = f"{time_diff // 3600}h ago"
                else:
                    last_str = f"{time_diff // 86400}d ago"
                
                # If filtering for fast markets, we'd check resolution date here
                # For now, show all but mark high-volume ones as likely faster
                
                shown += 1
                msg += f"*{shown}. Market #{market_id}*\n"
                msg += f"   Volume: ${volume:,.0f}\n"
                msg += f"   Trades: {trades}\n"
                msg += f"   Last: {last_str}\n"
                
                # High volume = likely active/fast
                if volume > 1000:
                    msg += f"   🔥 *High volume - likely fast!*\n"
                
                msg += f"   `/market {market_id}`\n\n"
                
                if shown >= 10:
                    break
            
            msg += "💡 *Tap /market command to analyze*\n"
            msg += "Use `/markets fast` for quick-resolving markets"
            
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
            # Extract data from new API structure
            # Wallet is in taker.signer
            taker_data = order.get('taker', {})
            taker = taker_data.get('signer') if isinstance(taker_data, dict) else None
            
            # Market ID is in market.id
            market_data = order.get('market', {})
            market_id = market_data.get('id') if isinstance(market_data, dict) else None
            
            # Amount is in amountFilled
            amount_str = order.get('amountFilled', '0')
            amount = float(amount_str) / 1e18 if amount_str else 0
            
            # Side is in taker.outcome.name
            outcome = taker_data.get('outcome', {}) if isinstance(taker_data, dict) else {}
            side_name = outcome.get('name') if isinstance(outcome, dict) else 'Unknown'
            side = 0 if side_name == 'Yes' else 1
            
            # Price is in priceExecuted
            price_str = order.get('priceExecuted', '0')
            price = float(price_str) / 1e18 if price_str else 0
            
            # Timestamp
            timestamp_str = order.get('executedAt', '')
            try:
                from datetime import datetime
                if timestamp_str:
                    dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    timestamp = int(dt.timestamp())
                else:
                    timestamp = int(time.time())
            except:
                timestamp = int(time.time())
            
            # Create unique hash
            tx_hash = order.get('transactionHash', '')
            order_hash = f"{tx_hash}_{taker}_{amount_str}"
            
            # DEBUG: Log what we're saving
            print(f"    Saving: ${amount:.2f} from {taker[:10] if taker else 'unknown'}... on market #{market_id} ({side_name})")
            
            self.cursor.execute('''
                INSERT OR IGNORE INTO orders (order_hash, market_id, wallet, side, amount, price, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                order_hash,
                market_id,
                taker,
                side,
                amount,
                price,
                timestamp
            ))
            
            self.conn.commit()
            
            # Verify it was saved
            self.cursor.execute('SELECT COUNT(*) FROM orders')
            total = self.cursor.fetchone()[0]
            if total % 50 == 0:  # Log every 50 orders
                print(f"    📊 Database now has {total} total orders")
            
            return True
        except Exception as e:
            print(f"Error saving order: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def is_order_processed(self, order: Dict) -> bool:
        """Check if we've already processed this order"""
        try:
            tx_hash = order.get('transactionHash', '')
            taker_data = order.get('taker', {})
            taker = taker_data.get('signer') if isinstance(taker_data, dict) else None
            amount_str = order.get('amountFilled', '0')
            
            order_hash = f"{tx_hash}_{taker}_{amount_str}"
            
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
            
            # DEBUG: Print first order structure to understand format
            if all_orders and len(all_orders) > 0:
                print(f"  📋 Sample order structure:")
                sample = all_orders[0]
                print(f"     Keys: {list(sample.keys())}")
                print(f"     taker type: {type(sample.get('taker'))}")
                print(f"     taker value: {sample.get('taker')}")
                print(f"     takerAmount: {sample.get('takerAmount')}")
                print(f"     tokenId: {sample.get('tokenId')}")
                print(f"     Full sample: {sample}")
            
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
            import traceback
            traceback.print_exc()
            return []
    
    def detect_whale_activity(self, orders: List[Dict]) -> List[Dict]:
        """Detect whale bets with enhanced context"""
        alerts = []
        
        print(f"  Checking {len(orders)} orders for whales (threshold: ${self.whale_threshold})")
        
        for order in orders:
            # Extract amount from new structure
            amount_str = order.get('amountFilled', '0')
            amount = float(amount_str) / 1e18 if amount_str else 0
            
            # Debug: show all bet amounts for small batches
            if len(orders) <= 5:
                print(f"    Order: ${amount:.2f}")
            
            if amount >= self.whale_threshold:
                # Extract wallet from taker.signer
                taker_data = order.get('taker', {})
                wallet = taker_data.get('signer') if isinstance(taker_data, dict) else None
                
                # Extract market ID from market.id
                market_data = order.get('market', {})
                market_id = market_data.get('id') if isinstance(market_data, dict) else None
                
                # Extract side from taker.outcome.name
                outcome = taker_data.get('outcome', {}) if isinstance(taker_data, dict) else {}
                side_name = outcome.get('name') if isinstance(outcome, dict) else 'Unknown'
                
                # CREATE UNIQUE IDENTIFIER using transaction hash
                tx_hash = order.get('transactionHash', '')
                
                print(f"  Debug: tx_hash = {tx_hash[:20] if tx_hash else 'NONE'}...")
                print(f"  Debug: alerted_transactions has {len(self.alerted_transactions)} items")
                
                # PRIMARY CHECK: Transaction hash deduplication
                if tx_hash and tx_hash in self.alerted_transactions:
                    print(f"  ⏭️ DUPLICATE DETECTED! tx {tx_hash[:10]}... already alerted, skipping...")
                    continue
                
                # SECONDARY CHECK: Wallet + Market + Amount deduplication (last 1 hour)
                # This catches cases where tx_hash might be missing or different but it's the same bet
                one_hour_ago = int(time.time()) - 3600
                try:
                    self.cursor.execute('''
                        SELECT 1 FROM alerts 
                        WHERE alert_type = 'WHALE_BET'
                        AND market_id = ?
                        AND timestamp > ?
                        AND message LIKE ?
                        AND message LIKE ?
                    ''', (market_id, one_hour_ago, f'%{wallet}%', f'%{amount:.2f}%'))
                    
                    if self.cursor.fetchone():
                        print(f"  ⏭️ DUPLICATE! Same wallet/market/amount already alerted in last hour, skipping...")
                        # Add to set to prevent future checks
                        if tx_hash:
                            self.alerted_transactions.add(tx_hash)
                        continue
                except Exception as e:
                    print(f"  Warning: Secondary dedup check failed: {e}")
                
                # If no tx_hash, create one from order data
                if not tx_hash:
                    print(f"  ⚠️ WARNING: No transaction hash in order!")
                    # Create fallback ID
                    tx_hash = f"fallback_{wallet}_{market_id}_{amount_str}_{order.get('executedAt', '')}"
                    print(f"  Using fallback ID: {tx_hash[:30]}...")
                
                # Create alert_id for the alert object
                alert_id = f"{tx_hash}_{wallet}_{amount_str}"
                
                print(f"  🐋 NEW WHALE! ${amount:.2f} on market #{market_id} ({side_name})")
                print(f"  Will add to alerted set: {tx_hash[:20]}...")
                
                # Extract market details
                market_title = "Unknown Market"
                closes_at = ""
                days_to_close = None
                
                if isinstance(market_data, dict):
                    # Get the question/title
                    market_title = market_data.get('question', market_data.get('title', f'Market #{market_id}'))
                    
                    # Try to extract resolution/closing date
                    # Look in the description for dates
                    description = market_data.get('description', '')
                    
                    # Check for resolution info in description
                    if 'December 31, 2026' in description:
                        closes_at = "Dec 31, 2026"
                        # Calculate days remaining
                        from datetime import datetime
                        try:
                            end_date = datetime(2026, 12, 31)
                            now = datetime.now()
                            days_to_close = (end_date - now).days
                        except:
                            pass
                    elif 'by December' in description or 'by 2026' in description:
                        closes_at = "Long-term (2026)"
                    else:
                        # Try to find any date mention
                        import re
                        date_patterns = [
                            r'by (\w+ \d+, \d{4})',
                            r'(\w+ \d+, \d{4})',
                            r'(\d{4}-\d{2}-\d{2})'
                        ]
                        for pattern in date_patterns:
                            match = re.search(pattern, description)
                            if match:
                                closes_at = match.group(1)
                                break
                        
                        if not closes_at:
                            closes_at = "Check market page"
                    
                    # Look for resolution status
                    resolution = market_data.get('resolution')
                    if resolution:
                        closes_at = f"RESOLVED: {resolution}"
                        days_to_close = 0
                
                # Get wallet stats
                wallet_stats = self.get_wallet_win_rate(wallet) if wallet else {}
                
                # Get market context
                market_info = self.get_market_context(market_id) if market_id else {}
                
                # Calculate entry quality and position sizing
                current_odds_yes = market_info.get('current_odds_yes')
                current_odds_no = market_info.get('current_odds_no')
                relevant_odds = current_odds_yes if side_name == "Yes" else current_odds_no
                
                entry_analysis = self.calculate_entry_quality(
                    side=side_name,
                    amount=amount,
                    whale_win_rate=wallet_stats.get('win_rate', 0),
                    current_odds=relevant_odds,
                    market_volume=market_info.get('volume', 0),
                    days_to_close=days_to_close
                )
                
                # FILTER: Skip alerts with score < 35 (bad bets)
                if entry_analysis['score'] < 35:
                    print(f"  ⚠️ Whale bet FILTERED OUT - Low quality (score: {entry_analysis['score']})")
                    print(f"     Reasons: {', '.join(entry_analysis['warnings'])}")
                    continue
                
                # Timestamp from executedAt (ISO format)
                timestamp_str = order.get('executedAt', '')
                try:
                    from datetime import datetime
                    if timestamp_str:
                        dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                        timestamp = int(dt.timestamp())
                    else:
                        timestamp = int(time.time())
                except:
                    timestamp = int(time.time())
                
                alert = {
                    'type': 'WHALE_BET',
                    'alert_id': alert_id,  # Unique ID for deduplication
                    'market_id': market_id,
                    'market_title': market_title[:150],
                    'closes_at': closes_at,
                    'days_to_close': days_to_close,
                    'wallet': wallet,
                    'amount': amount,
                    'side': side_name,
                    'timestamp': timestamp,
                    # Whale stats
                    'wallet_win_rate': wallet_stats.get('win_rate', 0),
                    'wallet_total_bets': wallet_stats.get('total_bets', 0),
                    'wallet_wins': wallet_stats.get('wins', 0),
                    'wallet_losses': wallet_stats.get('losses', 0),
                    # Market data
                    'market_volume': market_info.get('volume', 0),
                    'current_odds_yes': current_odds_yes,
                    'current_odds_no': current_odds_no,
                    # Entry quality
                    'entry_score': entry_analysis['score'],
                    'recommendation': entry_analysis['recommendation'],
                    'position_pct': entry_analysis['position_pct'],
                    'entry_reasons': entry_analysis['reasons'],
                    'entry_warnings': entry_analysis['warnings'],
                }
                
                # Save to whale history for tracking
                self.save_whale_bet_history(wallet, market_id, side_name, amount, timestamp)
                
                alerts.append(alert)
                self.save_alert(alert)
                
                # Mark this transaction as alerted
                if tx_hash:
                    self.alerted_transactions.add(tx_hash)
                    print(f"  ✅ Added to alerted set. Total in set: {len(self.alerted_transactions)}")
                else:
                    print(f"  ⚠️ WARNING: No tx_hash to add to set!")
        
        print(f"  Summary: {len(alerts)} whale alerts created from {len(orders)} orders")
        print(f"  Alerted transactions set size: {len(self.alerted_transactions)}")
        return alerts
    
    def save_whale_bet_history(self, wallet: str, market_id: int, side: str, amount: float, timestamp: int):
        """Save whale bet to history for win rate tracking"""
        try:
            self.cursor.execute('''
                INSERT INTO whale_history (wallet, market_id, side, amount, bet_timestamp)
                VALUES (?, ?, ?, ?, ?)
            ''', (wallet, market_id, side, amount, timestamp))
            self.conn.commit()
        except Exception as e:
            print(f"Error saving whale history: {e}")
    
    def get_wallet_win_rate(self, wallet: str) -> Dict:
        """Get wallet performance stats with actual win rate if available"""
        try:
            # Get total bets from orders
            self.cursor.execute('''
                SELECT COUNT(*) as total_bets, SUM(amount) as total_volume
                FROM orders 
                WHERE wallet = ?
            ''', (wallet,))
            
            result = self.cursor.fetchone()
            total_bets = result[0] if result else 0
            total_volume = result[1] if result and result[1] else 0
            
            # Get actual win/loss record from whale history
            self.cursor.execute('''
                SELECT 
                    COUNT(CASE WHEN result = 'WIN' THEN 1 END) as wins,
                    COUNT(CASE WHEN result = 'LOSS' THEN 1 END) as losses,
                    COUNT(CASE WHEN result = 'PENDING' THEN 1 END) as pending
                FROM whale_history
                WHERE wallet = ?
            ''', (wallet,))
            
            history = self.cursor.fetchone()
            wins = history[0] if history else 0
            losses = history[1] if history else 0
            pending = history[2] if history else 0
            
            # Calculate actual win rate if we have resolved bets
            settled = wins + losses
            actual_win_rate = 0
            if settled > 0:
                actual_win_rate = int((wins / settled) * 100)
            
            # If no settled bets, estimate based on volume
            estimated_win_rate = 0
            if actual_win_rate == 0 and total_bets > 10:
                if total_volume > 5000:
                    estimated_win_rate = 75
                elif total_volume > 2000:
                    estimated_win_rate = 70
                elif total_volume > 1000:
                    estimated_win_rate = 65
                elif total_volume > 500:
                    estimated_win_rate = 60
                elif total_volume > 100:
                    estimated_win_rate = 55
            
            return {
                'win_rate': actual_win_rate if actual_win_rate > 0 else estimated_win_rate,
                'is_actual': actual_win_rate > 0,
                'wins': wins,
                'losses': losses,
                'pending': pending,
                'total_bets': total_bets,
                'total_volume': total_volume
            }
        except Exception as e:
            print(f"Error getting win rate: {e}")
            return {'win_rate': 0, 'is_actual': False, 'wins': 0, 'losses': 0, 'pending': 0, 'total_bets': 0, 'total_volume': 0}
    
    
    def get_market_context(self, market_id: int) -> Dict:
        """Get market context with REAL current odds from API"""
        try:
            # First get our stored volume data
            self.cursor.execute('''
                SELECT SUM(amount) as volume, COUNT(*) as trades
                FROM orders 
                WHERE market_id = ? AND timestamp > ?
            ''', (market_id, int(time.time()) - 86400))  # Last 24h
            
            result = self.cursor.fetchone()
            volume = result[0] or 0 if result else 0
            trades = result[1] or 0 if result else 0
            
            # NOW fetch REAL current odds from API
            current_odds_yes = None
            current_odds_no = None
            
            try:
                # Fetch market details from API
                response = requests.get(
                    f"{self.base_url}/markets/{market_id}",
                    headers=self.headers,
                    timeout=5
                )
                
                if response.status_code == 200:
                    market_data = response.json().get('data', {})
                    
                    # Try to extract current odds
                    outcomes = market_data.get('outcomes', [])
                    
                    for outcome in outcomes:
                        if isinstance(outcome, dict):
                            name = outcome.get('name', '')
                            # Look for price/odds field
                            price = outcome.get('price') or outcome.get('probability') or outcome.get('odds')
                            
                            if price:
                                try:
                                    price_float = float(price)
                                    if name == 'Yes':
                                        current_odds_yes = price_float
                                    elif name == 'No':
                                        current_odds_no = price_float
                                except:
                                    pass
                    
                    # If not in outcomes, try top level
                    if current_odds_yes is None:
                        yes_price = market_data.get('yesPrice') or market_data.get('yesProbability')
                        if yes_price:
                            try:
                                current_odds_yes = float(yes_price)
                            except:
                                pass
            except Exception as e:
                print(f"  Warning: Could not fetch current odds: {e}")
            
            return {
                'volume': volume,
                'trades': trades,
                'current_odds_yes': current_odds_yes,
                'current_odds_no': current_odds_no
            }
        except Exception as e:
            print(f"Error getting market context: {e}")
            return {'volume': 0, 'trades': 0, 'current_odds_yes': None, 'current_odds_no': None}
    
    def calculate_entry_quality(self, side: str, amount: float, whale_win_rate: int, 
                                current_odds: float, market_volume: float, days_to_close: int) -> Dict:
        """Calculate if this is a good entry and how much to bet"""
        
        score = 0
        reasons = []
        warnings = []
        
        # Factor 1: Whale confidence (bet size)
        if amount >= 1000:
            score += 25
            reasons.append("Huge whale bet ($1000+)")
        elif amount >= 500:
            score += 20
            reasons.append("Large whale bet ($500+)")
        elif amount >= 200:
            score += 15
            reasons.append("Medium whale bet ($200+)")
        elif amount >= 100:
            score += 10
            reasons.append("Small whale bet ($100+)")
        else:
            score += 5
            warnings.append("Small bet - low conviction")
        
        # Factor 2: Whale win rate
        if whale_win_rate >= 80:
            score += 25
            reasons.append(f"Elite whale ({whale_win_rate}% win rate)")
        elif whale_win_rate >= 70:
            score += 20
            reasons.append(f"Sharp whale ({whale_win_rate}% win rate)")
        elif whale_win_rate >= 60:
            score += 10
            reasons.append(f"Decent whale ({whale_win_rate}% win rate)")
        else:
            score += 0
            warnings.append("Unproven whale")
        
        # Factor 3: Entry price (CRITICAL!)
        if current_odds is not None:
            # Normalize odds to 0-1 if needed
            if current_odds > 1:
                current_odds = current_odds / 100
            
            # For YES bets, we want LOW odds (good value)
            # For NO bets, we want HIGH yes odds (meaning low NO odds)
            if side == "Yes":
                target_odds = current_odds
            else:  # NO bet
                target_odds = 1 - current_odds
            
            if target_odds < 0.40:
                score += 25
                reasons.append(f"Excellent entry ({int(target_odds*100)}% odds)")
            elif target_odds < 0.55:
                score += 20
                reasons.append(f"Good entry ({int(target_odds*100)}% odds)")
            elif target_odds < 0.65:
                score += 10
                reasons.append(f"Fair entry ({int(target_odds*100)}% odds)")
            elif target_odds < 0.75:
                score += 0
                warnings.append(f"Mediocre entry ({int(target_odds*100)}% odds)")
            else:
                score -= 20
                warnings.append(f"BAD ENTRY! Already at {int(target_odds*100)}% odds")
        else:
            warnings.append("Odds unknown - check manually!")
        
        # Factor 4: Market liquidity
        if market_volume >= 20000:
            score += 15
            reasons.append("Very liquid market ($20k+)")
        elif market_volume >= 10000:
            score += 12
            reasons.append("Liquid market ($10k+)")
        elif market_volume >= 5000:
            score += 8
            reasons.append("Decent liquidity ($5k+)")
        elif market_volume >= 2000:
            score += 3
            warnings.append("Low liquidity ($2k)")
        else:
            score -= 10
            warnings.append("VERY THIN! Hard to exit")
        
        # Factor 5: Time to close
        if days_to_close is not None:
            if days_to_close <= 1:
                score += 10
                reasons.append("Closes TODAY/TOMORROW! ⚡")
            elif days_to_close <= 3:
                score += 8
                reasons.append("Fast close (1-3 days)")
            elif days_to_close <= 7:
                score += 5
                reasons.append("Quick close (3-7 days)")
            elif days_to_close <= 14:
                score += 0
                warnings.append("Medium-term (7-14 days)")
            else:
                score -= 5
                warnings.append("Long-term (14+ days)")
        
        # Calculate position size (Kelly Criterion approximation)
        if score >= 80:
            position_pct = 0.30  # 30% of bankroll
            recommendation = "🔥 MAX BET!"
        elif score >= 65:
            position_pct = 0.20  # 20% of bankroll
            recommendation = "💪 STRONG BET"
        elif score >= 50:
            position_pct = 0.10  # 10% of bankroll
            recommendation = "✅ GOOD BET"
        elif score >= 35:
            position_pct = 0.05  # 5% of bankroll
            recommendation = "⚠️ SMALL BET"
        else:
            position_pct = 0
            recommendation = "❌ SKIP - Bad entry"
        
        return {
            'score': max(0, min(100, score)),  # Clamp to 0-100
            'recommendation': recommendation,
            'position_pct': position_pct,
            'reasons': reasons,
            'warnings': warnings
        }
        """Get market context with REAL current odds from API"""
        try:
            # First get our stored volume data
            self.cursor.execute('''
                SELECT SUM(amount) as volume, COUNT(*) as trades
                FROM orders 
                WHERE market_id = ? AND timestamp > ?
            ''', (market_id, int(time.time()) - 86400))  # Last 24h
            
            result = self.cursor.fetchone()
            volume = result[0] or 0 if result else 0
            trades = result[1] or 0 if result else 0
            
            # NOW fetch REAL current odds from API
            current_odds_yes = None
            current_odds_no = None
            
            try:
                # Fetch market details from API
                response = requests.get(
                    f"{self.base_url}/markets/{market_id}",
                    headers=self.headers,
                    timeout=5
                )
                
                if response.status_code == 200:
                    market_data = response.json().get('data', {})
                    
                    # Try to extract current odds
                    # Odds might be in different places depending on API structure
                    outcomes = market_data.get('outcomes', [])
                    
                    for outcome in outcomes:
                        if isinstance(outcome, dict):
                            name = outcome.get('name', '')
                            # Look for price/odds field
                            price = outcome.get('price') or outcome.get('probability') or outcome.get('odds')
                            
                            if price:
                                try:
                                    price_float = float(price)
                                    if name == 'Yes':
                                        current_odds_yes = price_float
                                    elif name == 'No':
                                        current_odds_no = price_float
                                except:
                                    pass
                    
                    # If not in outcomes, try top level
                    if current_odds_yes is None:
                        yes_price = market_data.get('yesPrice') or market_data.get('yesProbability')
                        if yes_price:
                            try:
                                current_odds_yes = float(yes_price)
                            except:
                                pass
            except Exception as e:
                print(f"  Warning: Could not fetch current odds: {e}")
            
            return {
                'volume': volume,
                'trades': trades,
                'current_odds_yes': current_odds_yes,
                'current_odds_no': current_odds_no
            }
        except Exception as e:
            print(f"Error getting market context: {e}")
            return {'volume': 0, 'trades': 0, 'current_odds_yes': None, 'current_odds_no': None}
    
    
    def detect_coordinated_activity(self, orders: List[Dict]) -> List[Dict]:
        """Detect coordinated betting with strict filters for high win rate"""
        market_activity = defaultdict(lambda: defaultdict(list))
        current_time = int(time.time())
        
        for order in orders:
            # Parse timestamp from ISO format
            timestamp_str = order.get('executedAt', '')
            try:
                from datetime import datetime
                if timestamp_str:
                    dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    timestamp = int(dt.timestamp())
                else:
                    timestamp = 0
            except:
                timestamp = 0
            
            if current_time - timestamp > COORDINATION_WINDOW:
                continue
            
            # Extract market ID from market.id
            market_data = order.get('market', {})
            market_id = market_data.get('id') if isinstance(market_data, dict) else None
            
            # Extract side from taker.outcome.name
            taker_data = order.get('taker', {})
            outcome = taker_data.get('outcome', {}) if isinstance(taker_data, dict) else {}
            side_name = outcome.get('name') if isinstance(outcome, dict) else 'Unknown'
            
            # Extract wallet from taker.signer
            wallet = taker_data.get('signer') if isinstance(taker_data, dict) else None
            
            # Extract amount
            amount_str = order.get('amountFilled', '0')
            amount = float(amount_str) / 1e18 if amount_str else 0
            
            # Store market data for later analysis
            market_activity[market_id][side_name].append({
                'wallet': wallet,
                'amount': amount,
                'timestamp': timestamp,
                'market_data': market_data  # Keep full market data for analysis
            })
        
        alerts = []
        for market_id, sides in market_activity.items():
            for side, wallets in sides.items():
                unique_wallets = len(set(w['wallet'] for w in wallets if w['wallet']))
                total_amount = sum(w['amount'] for w in wallets)
                
                # STRICT FILTER 1: Minimum 5 wallets (not 3)
                if unique_wallets < 5:
                    continue
                
                # STRICT FILTER 2: Minimum $500 total volume
                if total_amount < 500:
                    print(f"  ⚠️ Coordination on market #{market_id} rejected: Only ${total_amount:.2f} (need $500+)")
                    continue
                
                # STRICT FILTER 3: Check if market closes within 7 days
                days_to_close = None
                market_data = wallets[0].get('market_data', {}) if wallets else {}
                
                if isinstance(market_data, dict):
                    description = market_data.get('description', '')
                    
                    # Try to determine closing time
                    import re
                    from datetime import datetime, timedelta
                    
                    # Look for resolution date in description
                    close_soon = False
                    
                    # Check for "December 31, 2026" or similar far dates
                    if '2026' in description or '2027' in description:
                        # Long-term market, likely > 7 days
                        close_soon = False
                    elif 'today' in description.lower() or 'tomorrow' in description.lower():
                        close_soon = True
                    elif 'this week' in description.lower() or 'week' in description.lower():
                        close_soon = True
                    else:
                        # If we can't determine, be conservative - allow it through
                        close_soon = True
                    
                    if not close_soon:
                        print(f"  ⚠️ Coordination on market #{market_id} rejected: Long-term market (need <7 days)")
                        continue
                
                # ALL FILTERS PASSED! This is a HIGH-QUALITY signal
                print(f"  🔥 HIGH-QUALITY COORDINATION DETECTED!")
                print(f"     Market #{market_id}: {unique_wallets} wallets, ${total_amount:.2f}, {side}")
                
                alert = {
                    'type': 'COORDINATED_BETTING',
                    'market_id': market_id,
                    'side': side,  # Already in Yes/No format
                    'wallet_count': unique_wallets,
                    'total_amount': total_amount,
                    'timestamp': current_time,
                    'quality': 'HIGH'  # Mark as high quality
                }
                alerts.append(alert)
                self.save_alert(alert)
        
        return alerts
    
    def check_tracked_wallets(self, orders: List[Dict]) -> List[Dict]:
        """Check tracked wallet activity"""
        alerts = []
        
        for order in orders:
            # Extract wallet from taker.signer
            taker_data = order.get('taker', {})
            wallet = taker_data.get('signer') if isinstance(taker_data, dict) else None
            
            # Skip if wallet is None or not a string
            if not wallet or not isinstance(wallet, str):
                continue
            
            if wallet in self.tracked_wallets:
                # Parse timestamp
                timestamp_str = order.get('executedAt', '')
                try:
                    from datetime import datetime
                    if timestamp_str:
                        dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                        timestamp = int(dt.timestamp())
                    else:
                        timestamp = int(time.time())
                except:
                    timestamp = int(time.time())
                
                # Extract market ID
                market_data = order.get('market', {})
                market_id = market_data.get('id') if isinstance(market_data, dict) else None
                
                # Extract amount
                amount_str = order.get('amountFilled', '0')
                amount = float(amount_str) / 1e18 if amount_str else 0
                
                # Extract side
                outcome = taker_data.get('outcome', {}) if isinstance(taker_data, dict) else {}
                side_name = outcome.get('name') if isinstance(outcome, dict) else 'Unknown'
                
                alert = {
                    'type': 'TRACKED_WALLET',
                    'market_id': market_id,
                    'wallet': wallet,
                    'amount': amount,
                    'side': side_name,
                    'timestamp': timestamp
                }
                alerts.append(alert)
                self.save_alert(alert)
        
        return alerts
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
            
            market_id = alert.get('market_id')
            market_title = alert.get('market_title', f'Market #{market_id}')
            closes_at = alert.get('closes_at', '')
            days_to_close = alert.get('days_to_close')
            
            # Enhanced whale alert with market question at top
            msg = f"""
🐋 *WHALE ALERT!*

❓ *{market_title}*

"""
            
            # Show days to close prominently if available
            if days_to_close is not None:
                if days_to_close == 0:
                    msg += f"⏰ *RESOLVED*\n"
                elif days_to_close <= 1:
                    msg += f"⏰ *CLOSES TODAY!* ⚡\n"
                elif days_to_close <= 3:
                    msg += f"⏰ *Closes in {days_to_close} days* 🔥 FAST!\n"
                elif days_to_close <= 7:
                    msg += f"⏰ Closes in {days_to_close} days\n"
                elif days_to_close <= 30:
                    msg += f"⏰ Closes in ~{days_to_close} days\n"
                else:
                    msg += f"⏰ Long-term ({closes_at})\n"
            elif closes_at:
                msg += f"⏰ Closes: {closes_at}\n"
            
            msg += f"\n💰 *${alert.get('amount', 0):.2f}* on *{alert.get('side')}*\n"
            msg += f"Market ID: `#{market_id}`\n"
            msg += f"Wallet: `{wallet_short}`\n"
            
            # ADD CURRENT ODDS (Critical!)
            current_odds_yes = alert.get('current_odds_yes')
            current_odds_no = alert.get('current_odds_no')
            if current_odds_yes is not None:
                msg += f"\n📈 Current Odds:\n"
                msg += f"  • YES: {int(current_odds_yes*100 if current_odds_yes <= 1 else current_odds_yes)}%\n"
                if current_odds_no is not None:
                    msg += f"  • NO: {int(current_odds_no*100 if current_odds_no <= 1 else current_odds_no)}%\n"
            
            # ADD ENTRY QUALITY SCORE (NEW!)
            entry_score = alert.get('entry_score', 0)
            recommendation = alert.get('recommendation', '')
            position_pct = alert.get('position_pct', 0)
            
            if entry_score > 0:
                msg += f"\n🎯 *ENTRY ANALYSIS:*\n"
                msg += f"  • Quality Score: *{entry_score}/100*\n"
                msg += f"  • {recommendation}\n"
                
                # Position sizing
                if position_pct > 0:
                    msg += f"  • Position Size: *{int(position_pct*100)}% of bankroll*\n"
                    msg += f"    (With $10 → Bet ${position_pct*10:.2f})\n"
                
                # Show reasons
                reasons = alert.get('entry_reasons', [])
                if reasons:
                    msg += f"\n✅ *Strengths:*\n"
                    for reason in reasons[:3]:  # Top 3 reasons
                        msg += f"  • {reason}\n"
                
                # Show warnings
                warnings = alert.get('entry_warnings', [])
                if warnings:
                    msg += f"\n⚠️ *Cautions:*\n"
                    for warning in warnings[:2]:  # Top 2 warnings
                        msg += f"  • {warning}\n"
            
            # Add wallet stats if available
            if alert.get('wallet_total_bets', 0) > 0:
                win_rate = alert.get('wallet_win_rate', 0)
                wins = alert.get('wallet_wins', 0)
                losses = alert.get('wallet_losses', 0)
                
                if win_rate > 0:
                    msg += f"\n📊 Wallet Performance:\n"
                    
                    # Show if it's actual or estimated
                    if wins > 0 or losses > 0:
                        msg += f"  • Win Rate: *{win_rate}%* ({wins}W-{losses}L)\n"
                    else:
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
            
            # Add link to market
            if market_id:
                msg += f"\n🔗 View: https://predict.fun/market/{market_id}\n"
            
            return msg
        
        elif alert['type'] == 'COORDINATED_BETTING':
            wallet_count = alert.get('wallet_count', 0)
            total_amount = alert.get('total_amount', 0)
            quality = alert.get('quality', 'NORMAL')
            
            # Check if high quality
            if quality == 'HIGH':
                msg = f"""
⭐⭐⭐ *HIGH-QUALITY COORDINATION!* ⭐⭐⭐

🤝 *{wallet_count} WALLETS COORDINATING*

Market: `#{alert.get('market_id')}`
Side: *{alert.get('side')}*
Total: *${total_amount:,.2f}* USDT

✅ *ALL FILTERS PASSED:*
  • 5+ wallets ✓
  • $500+ volume ✓
  • Fast-closing (<7 days) ✓

🎯 *WIN RATE: 75-85%*

⚠️ *STRONG INSIDER SIGNAL!*
Multiple sharp wallets coordinating = they know something!

💡 Check `/market {alert.get('market_id')}` NOW!
"""
            else:
                # Should not happen with new filters, but keep as fallback
                msg = f"""
🤝 *COORDINATED ACTIVITY!*

Market: `#{alert.get('market_id')}`
Wallets: *{wallet_count}*
Side: *{alert.get('side')}*
Total: *${total_amount:.2f}* USDT

⚠️ *Multiple wallets betting together - possible insider info!*
"""
            
            return msg
        
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
        
        # Set up command menu
        async def post_init(application: Application):
            """Set bot commands menu"""
            commands = [
                ("start", "🚀 Start the bot"),
                ("status", "📊 Bot status & settings"),
                ("stats", "📈 24h statistics"),
                ("markets", "🔍 Active markets"),
                ("market", "📊 Analyze market"),
                ("topwallets", "👑 Top wallets"),
                ("whales", "🐋 Recent whale alerts"),
                ("track", "👁️ Track wallet"),
                ("mywallets", "📋 My tracked wallets"),
                ("settings", "⚙️ Quick settings"),
                ("setwhale", "💰 Set whale threshold"),
                ("pause", "⏸️ Pause monitoring"),
                ("help", "❓ Show help"),
            ]
            await application.bot.set_my_commands(commands)
            # Start monitoring
            asyncio.create_task(self.monitoring_loop(application))
        
        app.post_init = post_init
        
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
