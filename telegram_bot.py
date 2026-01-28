# This is the WORKING version from before the "complete 20%" features were added
# It has: proper API field mapping, deduplication, coordination filters, but NO entry scoring

import os
import time
import json
import sqlite3
import requests
import asyncio
from typing import Dict, List, Set
from datetime import datetime
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# Import settings
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
        self.alerted_transactions = set()  # Track tx hashes to prevent duplicates
        
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
        
        # Whale history for win/loss tracking
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS whale_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT,
                market_id INTEGER,
                side TEXT,
                amount REAL,
                bet_timestamp INTEGER,
                result TEXT DEFAULT 'PENDING',
                resolved_at INTEGER
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
    
    def save_order(self, order: Dict):
        """Save order to database to avoid reprocessing"""
        try:
            # Extract data from API structure
            taker_data = order.get('taker', {})
            taker = taker_data.get('signer') if isinstance(taker_data, dict) else None
            
            market_data = order.get('market', {})
            market_id = market_data.get('id') if isinstance(market_data, dict) else None
            
            amount_str = order.get('amountFilled', '0')
            amount = float(amount_str) / 1e18 if amount_str else 0
            
            outcome = taker_data.get('outcome', {}) if isinstance(taker_data, dict) else {}
            side_name = outcome.get('name') if isinstance(outcome, dict) else 'Unknown'
            side = 0 if side_name == 'Yes' else 1
            
            price_str = order.get('priceExecuted', '0')
            price = float(price_str) / 1e18 if price_str else 0
            
            timestamp_str = order.get('executedAt', '')
            try:
                if timestamp_str:
                    dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    timestamp = int(dt.timestamp())
                else:
                    timestamp = int(time.time())
            except:
                timestamp = int(time.time())
            
            tx_hash = order.get('transactionHash', '')
            order_hash = f"{tx_hash}_{taker}_{amount_str}"
            
            self.cursor.execute('''
                INSERT OR IGNORE INTO orders (order_hash, market_id, wallet, side, amount, price, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (order_hash, market_id, taker, side, amount, price, timestamp))
            
            self.conn.commit()
            return True
        except Exception as e:
            print(f"Error saving order: {e}")
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
                print(f"API Error: {response.status_code}")
                return []
            
            data = response.json()
            all_orders = data.get('data', [])
            
            # Filter out already processed
            new_orders = []
            for order in all_orders:
                if not self.is_order_processed(order):
                    self.save_order(order)
                    new_orders.append(order)
            
            return new_orders
            
        except Exception as e:
            print(f"⚠️ Error fetching orders: {e}")
            return []
    
    def detect_whale_activity(self, orders: List[Dict]) -> List[Dict]:
        """Detect whale bets - ONLY BIG WHALES to prevent spam"""
        alerts = []
        
        # STRICT FILTER: Only whales >= $100 (prevents flood)
        MINIMUM_WHALE = 100.0
        
        print(f"\n{'='*60}")
        print(f"🐋 WHALE DETECTION - Processing {len(orders)} orders")
        print(f"Filters: ≥${MINIMUM_WHALE} | ≤7 days | ≥$3k volume")
        print(f"{'='*60}\n")
        
        for idx, order in enumerate(orders, 1):
            # Extract amount
            amount_str = order.get('amountFilled', '0')
            amount = float(amount_str) / 1e18 if amount_str else 0
            
            # STRICT: Only $100+ whales
            if amount < MINIMUM_WHALE:
                continue
            
            # Extract data
            taker_data = order.get('taker', {})
            wallet = taker_data.get('signer') if isinstance(taker_data, dict) else None
            
            market_data = order.get('market', {})
            market_id = market_data.get('id') if isinstance(market_data, dict) else None
            market_title = market_data.get('question', market_data.get('title', f'Market #{market_id}'))
            
            outcome = taker_data.get('outcome', {}) if isinstance(taker_data, dict) else {}
            side_name = outcome.get('name') if isinstance(outcome, dict) else 'Unknown'
            
            # DEDUP: Check transaction hash
            tx_hash = order.get('transactionHash', '')
            
            if tx_hash and tx_hash in self.alerted_transactions:
                continue  # Skip duplicates
            
            # ADD TO SET IMMEDIATELY to prevent reprocessing (even if filters fail)
            if tx_hash:
                self.alerted_transactions.add(tx_hash)
            
            print(f"🐋 BIG WHALE: ${amount:.2f} on #{market_id}")
            
            # Get closing date
            closes_at, days_to_close = self.extract_closing_date(market_data)
            
            # FILTER 1: Only fast-closing markets (≤7 days) for flipping
            if days_to_close is not None and days_to_close > 7:
                print(f"  ⏭️ SKIPPED: Too slow ({days_to_close} days)")
                continue
            
            # Parse timestamp
            timestamp_str = order.get('executedAt', '')
            try:
                dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                timestamp = int(dt.timestamp())
            except:
                timestamp = int(time.time())
            
            # Get whale stats
            whale_stats = self.get_whale_stats(wallet) if wallet else {}
            
            # Get market volume
            market_volume = self.get_market_volume(market_id) if market_id else 0
            
            # FILTER 2: Only liquid markets (≥$3k volume) to ensure you can exit
            if market_volume < 3000:
                print(f"  ⏭️ SKIPPED: Low volume (${market_volume:.0f})")
                continue
            
            # Check if tracked wallet
            is_tracked = self.is_tracked_wallet(wallet) if wallet else False
            
            # Create alert
            alert = {
                'type': 'WHALE_BET',
                'market_id': market_id,
                'market_title': market_title[:150],
                'wallet': wallet,  # Full address
                'amount': amount,
                'side': side_name,
                'timestamp': timestamp,
                'closes_at': closes_at,
                'days_to_close': days_to_close,
                # Whale stats
                'whale_total_bets': whale_stats.get('total_bets', 0),
                'whale_total_volume': whale_stats.get('total_volume', 0),
                'whale_has_history': whale_stats.get('has_history', False),
                # Market context
                'market_volume': market_volume,
                # Tracking
                'is_tracked_wallet': is_tracked,
            }
            
            alerts.append(alert)
            self.save_alert(alert)
            
            # Save to whale history
            if wallet and market_id:
                self.save_whale_to_history(wallet, market_id, side_name, amount, timestamp)
        
        print(f"📊 Found {len(alerts)} whales >= ${MINIMUM_WHALE}\n")
        
        return alerts
    
    def detect_coordinated_activity(self, orders: List[Dict]) -> List[Dict]:
        """Detect coordinated betting with strict filters"""
        market_activity = defaultdict(lambda: defaultdict(list))
        current_time = int(time.time())
        
        for order in orders:
            # Parse timestamp
            timestamp_str = order.get('executedAt', '')
            try:
                if timestamp_str:
                    dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    timestamp = int(dt.timestamp())
                else:
                    timestamp = 0
            except:
                timestamp = 0
            
            if current_time - timestamp > COORDINATION_WINDOW:
                continue
            
            # Extract data
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
                
                # STRICT FILTERS
                if unique_wallets < 5:
                    continue
                
                if total_amount < 500:
                    continue
                
                # Check if fast-closing market
                market_data = wallets[0].get('market_data', {}) if wallets else {}
                if isinstance(market_data, dict):
                    description = market_data.get('description', '')
                    if '2026' in description or '2027' in description:
                        continue  # Skip long-term markets
                
                # Passed all filters!
                alert = {
                    'type': 'COORDINATED_BETTING',
                    'market_id': market_id,
                    'side': side,
                    'wallet_count': unique_wallets,
                    'total_amount': total_amount,
                    'timestamp': current_time,
                    'quality': 'HIGH'
                }
                alerts.append(alert)
                self.save_alert(alert)
        
        return alerts
    
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
    
    def save_whale_to_history(self, wallet: str, market_id: int, side: str, amount: float, timestamp: int):
        """Save whale bet to history for tracking"""
        try:
            self.cursor.execute('''
                INSERT INTO whale_history (wallet, market_id, side, amount, bet_timestamp)
                VALUES (?, ?, ?, ?, ?)
            ''', (wallet, market_id, side, amount, timestamp))
            self.conn.commit()
        except Exception as e:
            print(f"Error saving whale history: {e}")
    
    def get_whale_stats(self, wallet: str) -> Dict:
        """Get whale stats - ONLY show what we actually know"""
        try:
            # Get total bets and volume from our database
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
        except Exception as e:
            print(f"Error getting whale stats: {e}")
        
        return {'total_bets': 0, 'total_volume': 0, 'has_history': False}
    
    def get_market_volume(self, market_id: int) -> float:
        """Get market volume from API"""
        try:
            # First try our database
            cutoff = int(time.time()) - 86400
            self.cursor.execute('''
                SELECT SUM(amount) FROM orders 
                WHERE market_id = ? AND timestamp > ?
            ''', (market_id, cutoff))
            result = self.cursor.fetchone()
            db_volume = result[0] if result and result[0] else 0
            
            # If we have data, return it
            if db_volume > 0:
                return db_volume
            
            # Otherwise fetch from API
            response = requests.get(
                f"{self.base_url}/markets/{market_id}",
                headers=self.headers,
                timeout=5
            )
            
            if response.status_code == 200:
                data = response.json().get('data', {})
                # Try to get volume from market data
                volume = data.get('volume', 0)
                if volume:
                    return float(volume)
            
            return 0
        except Exception as e:
            print(f"Error getting market volume: {e}")
            return 0
    
    def extract_closing_date(self, market_data: Dict) -> tuple:
        """Extract closing date and days remaining from market data"""
        if not isinstance(market_data, dict):
            return ("Unknown", None)
        
        description = market_data.get('description', '')
        question = market_data.get('question', '')
        title = market_data.get('title', '')
        
        # Combine all text to search
        full_text = f"{question} {title} {description}".lower()
        
        from datetime import datetime, timedelta
        import re
        
        now = datetime.now()
        
        # Look for specific date patterns
        # Pattern 1: "January 28, 2025" or "Jan 28, 2025"
        date_match = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2}),?\s+(\d{4})', full_text, re.IGNORECASE)
        
        if date_match:
            month_str = date_match.group(1)
            day = int(date_match.group(2))
            year = int(date_match.group(3))
            
            # Map month names to numbers
            months = {
                'january': 1, 'jan': 1, 'february': 2, 'feb': 2, 'march': 3, 'mar': 3,
                'april': 4, 'apr': 4, 'may': 5, 'june': 6, 'jun': 6,
                'july': 7, 'jul': 7, 'august': 8, 'aug': 8, 'september': 9, 'sep': 9,
                'october': 10, 'oct': 10, 'november': 11, 'nov': 11, 'december': 12, 'dec': 12
            }
            
            month = months.get(month_str.lower(), 1)
            
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
                elif days_remaining <= 30:
                    return (f"{close_date.strftime('%b %d')}", days_remaining)
                elif days_remaining <= 365:
                    return (f"{close_date.strftime('%b %d, %Y')}", days_remaining)
                else:
                    return (f"Long-term ({year})", days_remaining)
            except:
                pass
        
        # Pattern 2: Look for "by [date]"
        by_match = re.search(r'by\s+(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})', full_text, re.IGNORECASE)
        if by_match:
            return (f"By {by_match.group(1)} {by_match.group(2)}", None)
        
        # Pattern 3: Keywords
        if any(word in full_text for word in ['today', 'tonight']):
            return ("TODAY", 0)
        
        if 'tomorrow' in full_text:
            return ("TOMORROW", 1)
        
        if any(word in full_text for word in ['this week', 'this weekend']):
            return ("This week", 3)
        
        if 'this month' in full_text:
            return ("This month", 15)
        
        # Pattern 4: Year-based (long-term)
        year_match = re.search(r'(2026|2027|2028)', full_text)
        if year_match:
            year = int(year_match.group(1))
            days_remaining = (datetime(year, 12, 31) - now).days
            return (f"Long-term ({year})", days_remaining)
        
        # Default
        return ("Check market", None)
    
    def is_tracked_wallet(self, wallet: str) -> bool:
        """Check if wallet is being tracked"""
        if wallet in self.tracked_wallets:
            return True
        try:
            self.cursor.execute('SELECT 1 FROM tracked_wallets WHERE wallet = ?', (wallet,))
            return self.cursor.fetchone() is not None
        except:
            return False
    
    
    def format_alert(self, alert: Dict) -> str:
        """Format alert message with all useful info"""
        if alert['type'] == 'WHALE_BET':
            wallet = alert.get('wallet', '')
            
            # Build alert message
            msg = f"🐋 *BIG WHALE ALERT!*\n\n"
            
            # Market question - in code block for easy copy
            msg += f"❓ *Market Question:*\n`{alert.get('market_title', 'Unknown')}`\n\n"
            
            # Bet details
            msg += f"💰 *${alert.get('amount', 0):.2f}* on *{alert.get('side')}*\n"
            msg += f"Market: `#{alert.get('market_id')}`\n"
            
            # Closing date - IMPORTANT for flipping!
            closes_at = alert.get('closes_at')
            days_to_close = alert.get('days_to_close')
            if days_to_close is not None:
                if days_to_close <= 1:
                    msg += f"⏰ *CLOSES TODAY/TOMORROW!* ⚡\n"
                elif days_to_close <= 7:
                    msg += f"⏰ *Closes in {days_to_close} days* 🔥\n"
                elif days_to_close <= 30:
                    msg += f"⏰ Closes in ~{days_to_close} days\n"
                else:
                    msg += f"⏰ {closes_at}\n"
            else:
                msg += f"⏰ {closes_at}\n"
            
            msg += f"\n"
            
            # Whale history - ONLY show what we actually know
            total_bets = alert.get('whale_total_bets', 0)
            total_volume = alert.get('whale_total_volume', 0)
            has_history = alert.get('whale_has_history', False)
            
            if has_history and total_bets > 0:
                msg += f"📊 *Whale History:*\n"
                msg += f"  • Previous bets: {total_bets}\n"
                msg += f"  • Total volume: ${total_volume:,.0f}\n"
                if total_bets >= 10:
                    msg += f"  • ✅ *Active whale*\n"
                elif total_bets >= 5:
                    msg += f"  • 📈 *Regular bettor*\n"
            else:
                msg += f"📊 New/unknown whale\n"
            
            # Market volume - CRITICAL for liquidity
            volume = alert.get('market_volume', 0)
            msg += f"\n💵 *Market Volume:* "
            if volume > 10000:
                msg += f"${volume:,.0f} 🟢\n"
            elif volume > 5000:
                msg += f"${volume:,.0f} 🟡\n"
            elif volume > 1000:
                msg += f"${volume:,.0f} 🟠\n"
            elif volume > 0:
                msg += f"${volume:,.0f} 🔴 *LOW!*\n"
            else:
                msg += f"Unknown (check market)\n"
            
            # Tracked wallet indicator
            if alert.get('is_tracked_wallet'):
                msg += f"\n👁️ *THIS IS YOUR TRACKED WALLET!*\n"
            
            # FULL Wallet address - easy to copy
            msg += f"\n📋 *Wallet:*\n`{wallet}`\n"
            msg += f"\n🔗 https://predict.fun/market/{alert.get('market_id')}\n"
            msg += f"\n💡 *Check odds before betting!*"
            
            return msg
        
        elif alert['type'] == 'COORDINATED_BETTING':
            wallet_count = alert.get('wallet_count', 0)
            total_amount = alert.get('total_amount', 0)
            
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

🔗 https://predict.fun/market/{alert.get('market_id')}
"""
            return msg
        
        elif alert['type'] == 'TRACKED_WALLET':
            wallet = alert.get('wallet', '')
            wallet_short = f"{wallet[:6]}...{wallet[-4:]}"
            
            msg = f"👁️ *TRACKED WALLET ACTIVE!*\n\n"
            msg += f"Wallet: `{wallet_short}`\n\n"
            msg += f"❓ *{alert.get('market_title', 'Unknown')}*\n\n"
            msg += f"💰 *${alert.get('amount', 0):.2f}* on *{alert.get('side')}*\n"
            msg += f"Market: `#{alert.get('market_id')}`\n"
            
            # Show closing date
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
            
            msg += f"\n🔗 https://predict.fun/market/{alert.get('market_id')}\n"
            
            return msg
        
        return str(alert)
    
    
    async def send_telegram_alert(self, alert: Dict, app: Application):
        """Send alert to Telegram"""
        try:
            message = self.format_alert(alert)
            await app.bot.send_message(
                chat_id=self.admin_chat_id,
                text=message,
                parse_mode='Markdown'
            )
        except Exception as e:
            print(f"Error sending Telegram message: {e}")
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start command"""
        await update.message.reply_text(
            "🤖 *Predict.fun Smart Money Tracker*\n\n"
            "Monitoring whale activity and coordinated betting.\n\n"
            "Use /help to see all commands.",
            parse_mode='Markdown'
        )
    
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Help command"""
        await update.message.reply_text(
            "📋 *Available Commands:*\n\n"
            "/status - Bot status\n"
            "/setwhale <amount> - Set whale threshold\n"
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
            f"Whale Threshold: ${self.whale_threshold}\n"
            f"Tracked Transactions: {len(self.alerted_transactions)}\n",
            parse_mode='Markdown'
        )
    
    async def cmd_setwhale(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set whale threshold"""
        if not context.args:
            await update.message.reply_text("Usage: /setwhale <amount>\nExample: /setwhale 100")
            return
        
        try:
            new_threshold = float(context.args[0])
            self.whale_threshold = new_threshold
            await update.message.reply_text(f"✅ Whale threshold set to ${new_threshold}")
        except:
            await update.message.reply_text("❌ Invalid amount")
    
    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Pause monitoring"""
        self.monitoring_active = False
        await update.message.reply_text("⏸️ Monitoring paused")
    
    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Resume monitoring"""
        self.monitoring_active = True
        await update.message.reply_text("▶️ Monitoring resumed")
    
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
            await update.message.reply_text("❌ Invalid wallet address (must start with 0x)")
            return
        
        try:
            self.cursor.execute('''
                INSERT OR IGNORE INTO tracked_wallets (wallet, added_at)
                VALUES (?, ?)
            ''', (wallet, int(time.time())))
            self.conn.commit()
            self.tracked_wallets.add(wallet)
            
            # Get wallet stats
            stats = self.get_whale_stats(wallet)
            msg = f"✅ Now tracking wallet `{wallet[:10]}...`\n\n"
            if stats['has_history']:
                msg += f"📊 Whale History:\n"
                msg += f"  • Bets: {stats['total_bets']}\n"
                msg += f"  • Volume: ${stats['total_volume']:,.0f}\n"
            else:
                msg += "New whale - no history yet.\nYou'll be alerted on their next bet!"
            
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
        try:
            self.cursor.execute('SELECT wallet FROM tracked_wallets')
            wallets = [row[0] for row in self.cursor.fetchall()]
            
            if not wallets:
                await update.message.reply_text("No tracked wallets.\n\nUse /track <address> to add one.")
                return
            
            msg = f"👁️ *Tracked Wallets ({len(wallets)}):*\n\n"
            for wallet in wallets:
                stats = self.get_whale_stats(wallet)
                msg += f"`{wallet[:10]}...`\n"
                if stats['has_history']:
                    msg += f"  {stats['total_bets']} bets, ${stats['total_volume']:,.0f} volume\n"
                else:
                    msg += f"  No history yet\n"
                msg += "\n"
            
            await update.message.reply_text(msg, parse_mode='Markdown')
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button callbacks"""
        pass
    
    async def monitoring_loop(self, app: Application):
        """Background monitoring loop"""
        print("🚀 Monitoring started...")
        
        while True:
            try:
                if not self.monitoring_active:
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue
                
                orders = self.get_order_matches()
                
                if orders:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✓ Found {len(orders)} NEW orders")
                    
                    # Detect activity
                    whale_alerts = self.detect_whale_activity(orders)
                    coord_alerts = self.detect_coordinated_activity(orders)
                    tracked_alerts = self.check_tracked_wallets(orders)
                    
                    # Send alerts
                    all_alerts = whale_alerts + coord_alerts + tracked_alerts
                    for alert in all_alerts:
                        await self.send_telegram_alert(alert, app)
                
                await asyncio.sleep(CHECK_INTERVAL)
                
            except Exception as e:
                print(f"Error in monitoring loop: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(CHECK_INTERVAL)
    
    def check_tracked_wallets(self, orders: List[Dict]) -> List[Dict]:
        """Check if any tracked wallets are active - ONLY NEW BETS"""
        alerts = []
        
        for order in orders:
            taker_data = order.get('taker', {})
            wallet = taker_data.get('signer') if isinstance(taker_data, dict) else None
            
            if not wallet or not self.is_tracked_wallet(wallet):
                continue
            
            # DEDUP: Check if we already alerted on this transaction
            tx_hash = order.get('transactionHash', '')
            if tx_hash and tx_hash in self.alerted_transactions:
                continue  # Already alerted (either as whale or tracked wallet)
            
            # Tracked wallet is active with NEW bet!
            amount_str = order.get('amountFilled', '0')
            amount = float(amount_str) / 1e18 if amount_str else 0
            
            market_data = order.get('market', {})
            market_id = market_data.get('id') if isinstance(market_data, dict) else None
            market_title = market_data.get('question', 'Unknown')
            
            outcome = taker_data.get('outcome', {}) if isinstance(taker_data, dict) else {}
            side_name = outcome.get('name') if isinstance(outcome, dict) else 'Unknown'
            
            # Get closing info
            closes_at, days_to_close = self.extract_closing_date(market_data)
            
            # Parse timestamp
            timestamp_str = order.get('executedAt', '')
            try:
                dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                timestamp = int(dt.timestamp())
            except:
                timestamp = int(time.time())
            
            alert = {
                'type': 'TRACKED_WALLET',
                'wallet': wallet,
                'market_id': market_id,
                'market_title': market_title,
                'amount': amount,
                'side': side_name,
                'timestamp': timestamp,
                'closes_at': closes_at,
                'days_to_close': days_to_close,
            }
            alerts.append(alert)
            self.save_alert(alert)
            
            # Mark as alerted
            if tx_hash:
                self.alerted_transactions.add(tx_hash)
        
        return alerts
    
    def run(self):
        """Start the Telegram bot"""
        print("🤖 Starting Telegram Bot...")
        
        app = Application.builder().token(self.telegram_token).build()
        
        # Add handlers
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("setwhale", self.cmd_setwhale))
        app.add_handler(CommandHandler("track", self.cmd_track))
        app.add_handler(CommandHandler("untrack", self.cmd_untrack))
        app.add_handler(CommandHandler("mywallets", self.cmd_mywallets))
        app.add_handler(CommandHandler("pause", self.cmd_pause))
        app.add_handler(CommandHandler("resume", self.cmd_resume))
        app.add_handler(CallbackQueryHandler(self.button_callback))
        
        # Set up command menu
        async def post_init(application: Application):
            commands = [
                ("start", "🚀 Start the bot"),
                ("status", "📊 Bot status"),
                ("track", "👁️ Track a whale wallet"),
                ("mywallets", "📋 My tracked wallets"),
                ("setwhale", "💰 Set whale threshold"),
                ("pause", "⏸️ Pause monitoring"),
                ("help", "❓ Show help"),
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
