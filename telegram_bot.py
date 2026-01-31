import os
import time
import json
import sqlite3
import requests
import asyncio
from typing import Dict, List
from datetime import datetime
from collections import defaultdict
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# Import settings
from config import *

class TelegramPredictBot:
    """Telegram bot for Predict.fun whale tracking"""
    
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
        self.whale_threshold = 100  # Hardcoded to $100+
        self.min_coordinated = 5
        
        # Database
        self.init_database()
        
        # Tracking
        self.tracked_wallets = set()
        self.alerted_transactions = set()
        self.sent_telegram_alerts = set()  # Track what we've sent to Telegram
        
    def init_database(self):
        """Initialize SQLite database"""
        self.conn = sqlite3.connect('telegram_bot.db', check_same_thread=False)
        self.cursor = self.conn.cursor()
        
        # Orders table
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_hash TEXT UNIQUE,
                market_id INTEGER,
                wallet TEXT,
                side INTEGER,
                amount REAL,
                price REAL,
                timestamp INTEGER
            )
        ''')
        
        # Alerts table
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type TEXT,
                market_id INTEGER,
                message TEXT,
                timestamp INTEGER
            )
        ''')
        
        # Whale history
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS whale_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT,
                market_id INTEGER,
                side TEXT,
                amount REAL,
                bet_timestamp INTEGER
            )
        ''')
        
        # Tracked wallets
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS tracked_wallets (
                wallet TEXT PRIMARY KEY,
                added_at INTEGER
            )
        ''')
        
        self.conn.commit()
        
        # Load tracked wallets
        self.cursor.execute('SELECT wallet FROM tracked_wallets')
        self.tracked_wallets = set(row[0] for row in self.cursor.fetchall())
    
    def save_order(self, order: Dict):
        """Save order to database"""
        try:
            taker_data = order.get('taker', {})
            taker = taker_data.get('signer') if isinstance(taker_data, dict) else None
            
            market_data = order.get('market', {})
            market_id = market_data.get('id') if isinstance(market_data, dict) else None
            
            amount_str = order.get('amountFilled', '0')
            amount = float(amount_str) / 1e18 if amount_str else 0
            
            outcome = taker_data.get('outcome', {}) if isinstance(taker_data, dict) else {}
            side_name = outcome.get('name') if isinstance(outcome, dict) else 'Unknown'
            side = 0 if side_name == 'Yes' else 1
            
            tx_hash = order.get('transactionHash', '')
            order_hash = f"{tx_hash}_{taker}_{amount_str}"
            
            timestamp_str = order.get('executedAt', '')
            try:
                dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                timestamp = int(dt.timestamp())
            except:
                timestamp = int(time.time())
            
            self.cursor.execute('''
                INSERT OR IGNORE INTO orders (order_hash, market_id, wallet, side, amount, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (order_hash, market_id, taker, side, amount, timestamp))
            
            self.conn.commit()
        except Exception as e:
            print(f"Error saving order: {e}")
    
    def is_order_processed(self, order: Dict) -> bool:
        """Check if order already processed"""
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
    
    def get_order_matches(self) -> List[Dict]:
        """Fetch recent order matches"""
        try:
            response = requests.get(
                f"{self.base_url}/orders/matches",
                headers=self.headers,
                params={"limit": 100},
                timeout=10
            )
            
            if response.status_code != 200:
                return []
            
            data = response.json()
            all_orders = data.get('data', [])
            
            new_orders = []
            for order in all_orders:
                if not self.is_order_processed(order):
                    self.save_order(order)
                    new_orders.append(order)
            
            return new_orders
            
        except Exception as e:
            print(f"Error fetching orders: {e}")
            return []
    
    def extract_closing_date(self, market_data: Dict) -> tuple:
        """Extract closing date from market data"""
        if not isinstance(market_data, dict):
            return ("Unknown", None)
        
        description = market_data.get('description', '')
        question = market_data.get('question', '')
        full_text = f"{question} {description}".lower()
        
        from datetime import datetime, timedelta
        import re
        
        now = datetime.now()
        
        # Look for date patterns
        date_match = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2}),?\s+(\d{4})', full_text, re.IGNORECASE)
        
        if date_match:
            months = {
                'january': 1, 'jan': 1, 'february': 2, 'feb': 2, 'march': 3, 'mar': 3,
                'april': 4, 'apr': 4, 'may': 5, 'june': 6, 'jun': 6,
                'july': 7, 'jul': 7, 'august': 8, 'aug': 8, 'september': 9, 'sep': 9,
                'october': 10, 'oct': 10, 'november': 11, 'nov': 11, 'december': 12, 'dec': 12
            }
            
            month = months.get(date_match.group(1).lower(), 1)
            day = int(date_match.group(2))
            year = int(date_match.group(3))
            
            try:
                close_date = datetime(year, month, day)
                days_remaining = (close_date - now).days
                
                if days_remaining < 0:
                    return ("Already passed", 0)
                elif days_remaining == 0:
                    return ("TODAY", 0)
                elif days_remaining == 1:
                    return ("TOMORROW", 1)
                elif days_remaining <= 7:
                    return (f"{close_date.strftime('%b %d')}", days_remaining)
                else:
                    return (f"{close_date.strftime('%b %d, %Y')}", days_remaining)
            except:
                pass
        
        # Keywords
        if any(word in full_text for word in ['today', 'tonight']):
            return ("TODAY", 0)
        if 'tomorrow' in full_text:
            return ("TOMORROW", 1)
        if 'this week' in full_text:
            return ("This week", 3)
        
        # Long-term
        year_match = re.search(r'(2026|2027|2028)', full_text)
        if year_match:
            year = int(year_match.group(1))
            days_remaining = (datetime(year, 12, 31) - now).days
            return (f"Long-term ({year})", days_remaining)
        
        return ("Check market", None)
    
    def get_whale_stats(self, wallet: str) -> Dict:
        """Get whale stats from our database"""
        try:
            self.cursor.execute('''
                SELECT COUNT(*), SUM(amount)
                FROM orders
                WHERE wallet = ?
            ''', (wallet,))
            
            row = self.cursor.fetchone()
            if row:
                total_bets = row[0] or 0
                total_volume = row[1] or 0
                
                return {
                    'total_bets': total_bets,
                    'total_volume': total_volume,
                    'has_history': total_bets > 0
                }
        except:
            pass
        
        return {'total_bets': 0, 'total_volume': 0, 'has_history': False}
    
    def get_market_volume(self, market_id: int) -> float:
        """Get market volume from API"""
        try:
            # Try database first
            cutoff = int(time.time()) - 86400
            self.cursor.execute('''
                SELECT SUM(amount) FROM orders 
                WHERE market_id = ? AND timestamp > ?
            ''', (market_id, cutoff))
            result = self.cursor.fetchone()
            db_volume = result[0] if result and result[0] else 0
            
            if db_volume > 0:
                return db_volume
            
            # Fetch from API
            response = requests.get(
                f"{self.base_url}/markets/{market_id}",
                headers=self.headers,
                timeout=5
            )
            
            if response.status_code == 200:
                data = response.json().get('data', {})
                volume = data.get('volume', 0)
                if volume:
                    return float(volume)
            
            return 0
        except:
            return 0
    
    def save_whale_to_history(self, wallet: str, market_id: int, side: str, amount: float, timestamp: int):
        """Save whale bet to history"""
        try:
            self.cursor.execute('''
                INSERT INTO whale_history (wallet, market_id, side, amount, bet_timestamp)
                VALUES (?, ?, ?, ?, ?)
            ''', (wallet, market_id, side, amount, timestamp))
            self.conn.commit()
        except Exception as e:
            print(f"Error saving whale history: {e}")
    
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
    
    def detect_whale_activity(self, orders: List[Dict]) -> List[Dict]:
        """Detect big whale bets with filters"""
        alerts = []
        MINIMUM_WHALE = 100.0
        TIME_WINDOW = 5400  # 90 minutes in seconds
        
        current_time = int(time.time())
        
        print(f"\n{'='*60}")
        print(f"🐋 WHALE DETECTION - Processing {len(orders)} orders")
        print(f"Filters: ≥${MINIMUM_WHALE} | ≤7 days | ≤30 day cutoff | ≥$3k volume | ≤90 min old")
        print(f"{'='*60}\n")
        
        for order in orders:
            amount_str = order.get('amountFilled', '0')
            amount = float(amount_str) / 1e18 if amount_str else 0
            
            if amount < MINIMUM_WHALE:
                continue
            
            taker_data = order.get('taker', {})
            wallet = taker_data.get('signer') if isinstance(taker_data, dict) else None
            
            market_data = order.get('market', {})
            market_id = market_data.get('id') if isinstance(market_data, dict) else None
            market_title = market_data.get('question', market_data.get('title', f'Market #{market_id}'))
            market_status = market_data.get('status', 'UNKNOWN') if isinstance(market_data, dict) else 'UNKNOWN'
            
            # FILTER: Skip closed/resolved markets
            if market_status not in ['ACTIVE', 'REGISTERED', 'UNKNOWN']:
                print(f"  ⏭️ SKIPPED: Market is {market_status} (closed/resolved)")
                continue
            
            # Build proper URL from question (create slug)
            def create_slug(text):
                import re
                slug = text.lower()
                slug = re.sub(r'[^\w\s-]', '', slug)  # Remove special chars
                slug = re.sub(r'[\s_]+', '-', slug)    # Replace spaces with dashes
                slug = re.sub(r'^-+|-+$', '', slug)    # Remove leading/trailing dashes
                return slug[:100]  # Limit length
            
            market_slug = create_slug(market_title)
            market_url = f"https://predict.fun/{market_slug}"
            
            outcome = taker_data.get('outcome', {}) if isinstance(taker_data, dict) else {}
            side_name = outcome.get('name') if isinstance(outcome, dict) else 'Unknown'
            
            # Parse timestamp
            timestamp_str = order.get('executedAt', '')
            try:
                dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                timestamp = int(dt.timestamp())
            except:
                timestamp = int(time.time())
            
            # AGE FILTER: Only process bets from last 90 minutes
            bet_age = current_time - timestamp
            if bet_age > TIME_WINDOW:
                print(f"  ⏭️ SKIPPED: Too old ({bet_age//60} minutes ago)")
                continue
            
            # DEDUP - WITH DEBUG LOGGING
            tx_hash = order.get('transactionHash', '')
            
            # DEBUG: Show transaction details
            print(f"\n{'─'*50}")
            print(f"🔍 DEBUGGING TRANSACTION:")
            print(f"  Market: #{market_id}")
            print(f"  Amount: ${amount:.2f}")
            print(f"  Wallet: {wallet[:10] if wallet else 'None'}...")
            print(f"  TX Hash: {tx_hash if tx_hash else 'EMPTY/NULL'}")
            print(f"  TX in set? {tx_hash in self.alerted_transactions if tx_hash else 'N/A (no hash)'}")
            print(f"  Set size: {len(self.alerted_transactions)}")
            
            # Check if already alerted
            if tx_hash and tx_hash in self.alerted_transactions:
                print(f"  ❌ DUPLICATE DETECTED - Skipping!")
                print(f"{'─'*50}\n")
                continue
            
            # Add to set IMMEDIATELY
            if tx_hash:
                self.alerted_transactions.add(tx_hash)
                print(f"  ✅ Added to dedup set (new size: {len(self.alerted_transactions)})")
            else:
                print(f"  ⚠️ WARNING: No TX hash - cannot deduplicate!")
            
            print(f"{'─'*50}\n")
            
            print(f"🐋 BIG WHALE: ${amount:.2f} on #{market_id} ({bet_age//60} min ago)")
            
            # Get closing date
            closes_at, days_to_close = self.extract_closing_date(market_data)
            
            # FILTER 1: Only ≤30 days (ignore long-term markets completely)
            if days_to_close is not None and days_to_close > 30:
                print(f"  ⏭️ SKIPPED: Too far away ({days_to_close} days) - ignoring long-term market")
                continue
            
            # FILTER 2: Only ≤7 days for flipping
            if days_to_close is not None and days_to_close > 7:
                print(f"  ⏭️ SKIPPED: Too slow for flipping ({days_to_close} days)")
                continue
            
            # ⭐ SAVE WHALE TO DATABASE FIRST (before getting stats)
            if wallet and market_id:
                self.save_whale_to_history(wallet, market_id, side_name, amount, timestamp)
                print(f"  💾 Saved to database")
            
            # ⭐ NOW GET FRESH STATS (includes this bet!)
            whale_stats = self.get_whale_stats(wallet) if wallet else {}
            market_volume = self.get_market_volume(market_id) if market_id else 0
            
            print(f"  📊 LIVE Stats - Whale: {whale_stats.get('total_bets', 0)} bets, ${whale_stats.get('total_volume', 0):.0f}")
            print(f"  📊 LIVE Stats - Market: ${market_volume:.0f} volume")
            
            # FILTER 2: Only ≥$3k volume
            if market_volume < 3000:
                print(f"  ⏭️ SKIPPED: Low volume (${market_volume:.0f})")
                continue
            
            is_tracked = wallet in self.tracked_wallets if wallet else False
            
            alert = {
                'type': 'WHALE_BET',
                'market_id': market_id,
                'market_title': market_title[:150],
                'market_url': market_url,
                'market_status': market_status,
                'wallet': wallet,
                'amount': amount,
                'side': side_name,
                'timestamp': timestamp,
                'closes_at': closes_at,
                'days_to_close': days_to_close,
                'whale_total_bets': whale_stats.get('total_bets', 0),
                'whale_total_volume': whale_stats.get('total_volume', 0),
                'whale_has_history': whale_stats.get('has_history', False),
                'market_volume': market_volume,
                'is_tracked_wallet': is_tracked,
            }
            
            alerts.append(alert)
            self.save_alert(alert)
            
            print(f"  ✅ ALERT CREATED with LIVE data!\n")
        
        print(f"📊 Found {len(alerts)} whales passing all filters\n")
        return alerts
    
    def detect_coordinated_activity(self, orders: List[Dict]) -> List[Dict]:
        """Detect coordinated betting"""
        market_activity = defaultdict(lambda: defaultdict(list))
        current_time = int(time.time())
        
        for order in orders:
            timestamp_str = order.get('executedAt', '')
            try:
                dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                timestamp = int(dt.timestamp())
            except:
                timestamp = 0
            
            if current_time - timestamp > 3600:
                continue
            
            market_data = order.get('market', {})
            market_id = market_data.get('id') if isinstance(market_data, dict) else None
            
            taker_data = order.get('taker', {})
            outcome = taker_data.get('outcome', {}) if isinstance(taker_data, dict) else {}
            side_name = outcome.get('name') if isinstance(outcome, dict) else 'Unknown'
            wallet = taker_data.get('signer') if isinstance(taker_data, dict) else None
            
            amount_str = order.get('amountFilled', '0')
            amount = float(amount_str) / 1e18 if amount_str else 0
            
            market_activity[market_id][side_name].append({
                'wallet': wallet,
                'amount': amount,
                'timestamp': timestamp,
                'market_data': market_data
            })
        
        alerts = []
        for market_id, sides in market_activity.items():
            for side, wallets in sides.items():
                unique_wallets = len(set(w['wallet'] for w in wallets if w['wallet']))
                total_amount = sum(w['amount'] for w in wallets)
                
                if unique_wallets < 5:
                    continue
                
                if total_amount < 500:
                    continue
                
                market_data = wallets[0].get('market_data', {}) if wallets else {}
                if isinstance(market_data, dict):
                    description = market_data.get('description', '')
                    if '2026' in description or '2027' in description:
                        continue
                
                alert = {
                    'type': 'COORDINATED_BETTING',
                    'market_id': market_id,
                    'side': side,
                    'wallet_count': unique_wallets,
                    'total_amount': total_amount,
                    'timestamp': current_time,
                }
                alerts.append(alert)
                self.save_alert(alert)
        
        return alerts
    
    def format_alert(self, alert: Dict) -> str:
        """Format alert message"""
        if alert['type'] == 'WHALE_BET':
            wallet = alert.get('wallet', '')
            
            msg = f"🐋 *BIG WHALE ALERT!*\n\n"
            msg += f"❓ *Market Question:*\n`{alert.get('market_title', 'Unknown')}`\n\n"
            msg += f"💰 *${alert.get('amount', 0):.2f}* on *{alert.get('side')}*\n"
            msg += f"Market: `#{alert.get('market_id')}`\n"
            
            closes_at = alert.get('closes_at')
            days_to_close = alert.get('days_to_close')
            if days_to_close is not None:
                if days_to_close <= 1:
                    msg += f"⏰ *{closes_at}* ⚡\n"
                elif days_to_close <= 7:
                    msg += f"⏰ *{closes_at}* ({days_to_close} days) 🔥\n"
                else:
                    msg += f"⏰ {closes_at}\n"
            else:
                msg += f"⏰ {closes_at}\n"
            
            msg += f"\n"
            
            total_bets = alert.get('whale_total_bets', 0)
            total_volume = alert.get('whale_total_volume', 0)
            has_history = alert.get('whale_has_history', False)
            
            if has_history and total_bets > 0:
                msg += f"📊 *Whale History:*\n"
                msg += f"  • Previous bets: {total_bets}\n"
                msg += f"  • Total volume: ${total_volume:,.0f}\n"
                if total_bets >= 10:
                    msg += f"  • ✅ *Active whale*\n"
            else:
                msg += f"📊 New/unknown whale\n"
            
            volume = alert.get('market_volume', 0)
            msg += f"\n💵 *Market Volume:* "
            if volume > 10000:
                msg += f"${volume:,.0f} 🟢\n"
            elif volume > 5000:
                msg += f"${volume:,.0f} 🟡\n"
            elif volume > 1000:
                msg += f"${volume:,.0f} 🟠\n"
            else:
                msg += f"${volume:,.0f}\n"
            
            if alert.get('is_tracked_wallet'):
                msg += f"\n👁️ *YOUR TRACKED WALLET!*\n"
            
            msg += f"\n📋 *Wallet:*\n`{wallet}`\n"
            msg += f"\n🔗 {alert.get('market_url', 'https://predict.fun')}\n"
            msg += f"\n💡 *Check odds before betting!*"
            
            return msg
        
        elif alert['type'] == 'COORDINATED_BETTING':
            wallet_count = alert.get('wallet_count', 0)
            total_amount = alert.get('total_amount', 0)
            
            msg = f"⭐⭐⭐ *COORDINATION ALERT!* ⭐⭐⭐\n\n"
            msg += f"🤝 *{wallet_count} WALLETS COORDINATING*\n\n"
            msg += f"Market: `#{alert.get('market_id')}`\n"
            msg += f"Side: *{alert.get('side')}*\n"
            msg += f"Total: *${total_amount:,.2f}* USDT\n\n"
            msg += f"✅ *Filters Passed:*\n"
            msg += f"  • 5+ wallets ✓\n"
            msg += f"  • $500+ volume ✓\n"
            msg += f"  • Fast-closing ✓\n\n"
            msg += f"🎯 *Strong Signal!*\n\n"
            msg += f"🔗 {alert.get('market_url', 'https://predict.fun')}"
            
            return msg
        
        return str(alert)
    
    async def send_telegram_alert(self, alert: Dict, app: Application):
        """Send alert to Telegram with deduplication"""
        try:
            # CREATE UNIQUE ALERT ID
            alert_id = f"{alert.get('market_id')}_{alert.get('wallet', '')}_{alert.get('amount', 0):.2f}_{alert.get('timestamp', 0)}"
            
            # CHECK IF ALREADY SENT
            if alert_id in self.sent_telegram_alerts:
                print(f"⏭️ TELEGRAM SKIP: Already sent alert for {alert_id}")
                return
            
            # MARK AS SENT IMMEDIATELY
            self.sent_telegram_alerts.add(alert_id)
            
            # Format and send
            message = self.format_alert(alert)
            
            print(f"📤 SENDING TO TELEGRAM: Market #{alert.get('market_id')} - ${alert.get('amount', 0):.2f}")
            
            await app.bot.send_message(
                chat_id=self.admin_chat_id,
                text=message,
                parse_mode='Markdown'
            )
            
            # RATE LIMITING: Wait 2 seconds between alerts
            await asyncio.sleep(2)
            
            print(f"✅ TELEGRAM SENT: Alert ID {alert_id}")
            
        except Exception as e:
            print(f"❌ Error sending Telegram alert: {e}")
            # Remove from sent set if failed so we can retry
            self.sent_telegram_alerts.discard(alert_id)
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start command"""
        await update.message.reply_text(
            "🤖 *Predict.fun Whale Tracker*\n\n"
            "Tracking big whales and coordination!\n\n"
            "Use /help to see commands.",
            parse_mode='Markdown'
        )
    
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Help command"""
        await update.message.reply_text(
            "📋 *Commands:*\n\n"
            "/status - Bot status\n"
            "/track <wallet> - Track a whale\n"
            "/mywallets - Your tracked wallets\n"
            "/untrack <wallet> - Stop tracking\n"
            "/pause - Pause monitoring\n"
            "/resume - Resume monitoring\n",
            parse_mode='Markdown'
        )
    
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Status command"""
        status = "🟢 Active" if self.monitoring_active else "🔴 Paused"
        await update.message.reply_text(
            f"📊 *Bot Status*\n\n"
            f"Status: {status}\n"
            f"Whale Threshold: $100+\n"
            f"Tracked Wallets: {len(self.tracked_wallets)}\n"
            f"Tracked Transactions: {len(self.alerted_transactions)}\n",
            parse_mode='Markdown'
        )
    
    async def cmd_track(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Track a whale wallet"""
        if not context.args:
            await update.message.reply_text(
                "Usage: /track <wallet_address>\n\n"
                "Example: /track 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0"
            )
            return
        
        wallet = context.args[0]
        if not wallet.startswith('0x'):
            await update.message.reply_text("❌ Invalid wallet (must start with 0x)")
            return
        
        try:
            self.cursor.execute('''
                INSERT OR IGNORE INTO tracked_wallets (wallet, added_at)
                VALUES (?, ?)
            ''', (wallet, int(time.time())))
            self.conn.commit()
            self.tracked_wallets.add(wallet)
            
            stats = self.get_whale_stats(wallet)
            msg = f"✅ Now tracking `{wallet[:10]}...`\n\n"
            if stats['has_history']:
                msg += f"📊 Whale History:\n"
                msg += f"  • Bets: {stats['total_bets']}\n"
                msg += f"  • Volume: ${stats['total_volume']:,.0f}\n"
            else:
                msg += "New whale - no history yet."
            
            await update.message.reply_text(msg, parse_mode='Markdown')
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")
    
    async def cmd_untrack(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Untrack a wallet"""
        if not context.args:
            await update.message.reply_text("Usage: /untrack <wallet_address>")
            return
        
        wallet = context.args[0]
        try:
            self.cursor.execute('DELETE FROM tracked_wallets WHERE wallet = ?', (wallet,))
            self.conn.commit()
            self.tracked_wallets.discard(wallet)
            await update.message.reply_text(f"✅ Stopped tracking `{wallet[:10]}...`", parse_mode='Markdown')
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")
    
    async def cmd_mywallets(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List tracked wallets"""
        if not self.tracked_wallets:
            await update.message.reply_text("No tracked wallets.\n\nUse /track <address> to add one.")
            return
        
        msg = f"👁️ *Tracked Wallets ({len(self.tracked_wallets)}):*\n\n"
        for wallet in self.tracked_wallets:
            stats = self.get_whale_stats(wallet)
            msg += f"`{wallet[:10]}...`\n"
            if stats['has_history']:
                msg += f"  {stats['total_bets']} bets, ${stats['total_volume']:,.0f}\n"
            msg += "\n"
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    
    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Pause monitoring"""
        self.monitoring_active = False
        await update.message.reply_text("⏸️ Monitoring paused")
    
    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Resume monitoring"""
        self.monitoring_active = True
        await update.message.reply_text("▶️ Monitoring resumed")
    
    async def monitoring_loop(self, app: Application):
        """Background monitoring loop"""
        print("🚀 Monitoring started...")
        
        iteration = 0
        
        while True:
            try:
                if not self.monitoring_active:
                    await asyncio.sleep(30)
                    continue
                
                iteration += 1
                
                # Clean up sent alerts set every hour (prevent memory bloat)
                if iteration % 120 == 0:  # Every 120 iterations = 1 hour at 30sec intervals
                    old_size = len(self.sent_telegram_alerts)
                    self.sent_telegram_alerts.clear()
                    print(f"🧹 Cleaned sent alerts set (was {old_size}, now 0)")
                
                orders = self.get_order_matches()
                
                if orders:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✓ Found {len(orders)} NEW orders")
                    
                    whale_alerts = self.detect_whale_activity(orders)
                    coord_alerts = self.detect_coordinated_activity(orders)
                    
                    all_alerts = whale_alerts + coord_alerts
                    for alert in all_alerts:
                        await self.send_telegram_alert(alert, app)
                
                await asyncio.sleep(30)
                
            except Exception as e:
                print(f"Error in monitoring loop: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(30)
    
    def run(self):
        """Start the bot"""
        print("🤖 Starting Telegram Bot...")
        
        app = Application.builder().token(self.telegram_token).build()
        
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("track", self.cmd_track))
        app.add_handler(CommandHandler("untrack", self.cmd_untrack))
        app.add_handler(CommandHandler("mywallets", self.cmd_mywallets))
        app.add_handler(CommandHandler("pause", self.cmd_pause))
        app.add_handler(CommandHandler("resume", self.cmd_resume))
        
        async def post_init(application: Application):
            commands = [
                ("start", "🚀 Start bot"),
                ("status", "📊 Bot status"),
                ("track", "👁️ Track whale"),
                ("mywallets", "📋 Tracked wallets"),
                ("help", "❓ Help"),
            ]
            await application.bot.set_my_commands(commands)
            asyncio.create_task(self.monitoring_loop(application))
        
        app.post_init = post_init
        
        print("✅ Bot is running!")
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        exit(1)
    
    bot = TelegramPredictBot(
        predict_api_key=PREDICT_API_KEY,
        telegram_token=TELEGRAM_TOKEN,
        chat_id=TELEGRAM_CHAT_ID
    )
    
    bot.run()
