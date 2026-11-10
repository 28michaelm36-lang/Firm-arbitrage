#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.Gui;
using NinjaTrader.NinjaScript;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
	public enum BoldHarvestPhase { Evaluation, Funded }
	public enum BoldHarvestDrawdown { Static, EndOfDayTrailing, IntradayTrailing }
	public enum BoldHarvestEntry { NoiseBreakout, FirstCandle, OpeningRange, GapFade, AlwaysLong, AlwaysShort }

	/// <summary>
	/// BoldHarvest - asymmetry2 for one prop-firm account.
	///
	/// The account sets the bracket, not the market:
	///   - Evaluation: one trade aimed at the whole remaining profit target, risking the
	///     whole remaining drawdown cushion (bold play).
	///   - Funded: the same trade aimed at the balance that unlocks your next payout
	///     ("Goal ($ above start)"), then minimum-size trades only, to collect winning days.
	/// A consistency rule is handled with "Max profit per day"; a daily loss limit caps the risk.
	///
	/// The entry only picks the time and the direction. NoiseBreakout and FirstCandle were the
	/// two entries that beat a coin flip in the research; OpeningRange is asymmetry1's entry.
	///
	/// The stop distance sizes the trade: Stop (x average range) times the average session
	/// range of the last 14 sessions, narrower late in the day. Contracts = risk / (stop x
	/// point value), so on NQ a $2,000 risk is usually 1-3 contracts.
	///
	/// Run on 1-minute bars. Backtest with Order Fill Resolution = High (1 tick).
	/// LIVE: set "Balance now" and "Max loss level now" from the firm's dashboard before
	/// enabling. On the first real-time bar the strategy drops everything it simulated on
	/// historical bars and starts from those two numbers. It does not withdraw payouts for
	/// you: after a payout, update "Balance now" and restart it.
	/// </summary>
	public class BoldHarvest : Strategy
	{
		private const int Lookback = 14;
		private const int Checks = 12;				// 10:00, 10:30, ... 15:30

		// account
		private double	floor;
		private double	hwm;
		private bool	breached;
		private double	baselinePnl;
		private double	dayStartBalance;
		private int		tradesToday;
		private int		tradingDays;
		private bool	tradedThisDay;
		private int		lastDay;

		// session
		private bool	haveOpen;
		private double	sessionOpen;
		private double	prevClose;
		private double	lastRthClose;
		private double	dayHigh;
		private double	dayLow;
		private double	orHigh;
		private double	orLow;
		private bool	haveFirstCandle;
		private double	firstCandleClose;
		private int		orEndTime;
		private int		firstCandleEndTime;

		// history for the stop width and the noise band
		private double[]	ranges;
		private int			rangeCount;
		private int[]		checkTimes;
		private double[][]	moves;
		private int			moveCount;
		private double[]	todayMoves;

		// the open trade
		private double	stopPrice;

		#region Parameters
		[NinjaScriptProperty]
		[Display(Name = "Phase", Order = 1, GroupName = "1. Account")]
		public BoldHarvestPhase Phase { get; set; }

		[NinjaScriptProperty]
		[Range(1, int.MaxValue)]
		[Display(Name = "Starting balance ($)", Description = "The account's nominal size, e.g. 50000", Order = 2, GroupName = "1. Account")]
		public double StartingBalance { get; set; }

		[NinjaScriptProperty]
		[Range(0, int.MaxValue)]
		[Display(Name = "Balance now ($)", Description = "Live: the balance on the firm's dashboard. 0 = starting balance", Order = 3, GroupName = "1. Account")]
		public double BalanceNow { get; set; }

		[NinjaScriptProperty]
		[Range(0, int.MaxValue)]
		[Display(Name = "Max loss level now ($)", Description = "Live: the liquidation balance on the firm's dashboard. 0 = starting balance minus the drawdown", Order = 4, GroupName = "1. Account")]
		public double FloorNow { get; set; }

		[NinjaScriptProperty]
		[Range(1, int.MaxValue)]
		[Display(Name = "Drawdown ($)", Order = 5, GroupName = "1. Account")]
		public double DrawdownDollars { get; set; }

		[NinjaScriptProperty]
		[Display(Name = "Drawdown type", Order = 6, GroupName = "1. Account")]
		public BoldHarvestDrawdown DrawdownType { get; set; }

		[NinjaScriptProperty]
		[Display(Name = "Drawdown locks", Description = "The trailing level stops rising at starting balance + lock offset", Order = 7, GroupName = "1. Account")]
		public bool DrawdownLocks { get; set; }

		[NinjaScriptProperty]
		[Range(0, int.MaxValue)]
		[Display(Name = "Lock offset ($)", Order = 8, GroupName = "1. Account")]
		public double LockOffsetDollars { get; set; }

		[NinjaScriptProperty]
		[Range(0, int.MaxValue)]
		[Display(Name = "Daily loss limit ($)", Description = "0 = none", Order = 9, GroupName = "1. Account")]
		public double DailyLossLimit { get; set; }

		[NinjaScriptProperty]
		[Range(1, int.MaxValue)]
		[Display(Name = "Max contracts", Order = 10, GroupName = "1. Account")]
		public int MaxContracts { get; set; }

		[NinjaScriptProperty]
		[Range(1, int.MaxValue)]
		[Display(Name = "Goal ($ above start)", Description = "Evaluation: the profit target. Funded: the profit that unlocks the payout you want", Order = 1, GroupName = "2. Goal")]
		public double GoalDollars { get; set; }

		[NinjaScriptProperty]
		[Range(0, int.MaxValue)]
		[Display(Name = "Max profit per day ($)", Description = "For consistency rules, e.g. 1470 for a 50% rule on a $3,000 target. 0 = no cap", Order = 2, GroupName = "2. Goal")]
		public double MaxDayProfit { get; set; }

		[NinjaScriptProperty]
		[Range(0, int.MaxValue)]
		[Display(Name = "After the goal: daily target ($)", Description = "One small trade a day risking and targeting this much, to collect trading or winning days. 0 = stop trading", Order = 3, GroupName = "2. Goal")]
		public double CoastDollars { get; set; }

		[NinjaScriptProperty]
		[Range(0.05, 1.0)]
		[Display(Name = "Max risk (fraction of cushion)", Description = "1 = bold play. Lower it if the firm bans all-in trades", Order = 1, GroupName = "3. Trade")]
		public double MaxRiskFraction { get; set; }

		[NinjaScriptProperty]
		[Range(0.1, 100.0)]
		[Display(Name = "Min reward:risk", Description = "Never risk more than need / this", Order = 2, GroupName = "3. Trade")]
		public double MinOdds { get; set; }

		[NinjaScriptProperty]
		[Range(0.01, 5.0)]
		[Display(Name = "Stop (x average range)", Description = "Stop distance as a multiple of the 14-session average range", Order = 3, GroupName = "3. Trade")]
		public double StopRangeMultiple { get; set; }

		[NinjaScriptProperty]
		[Range(0, int.MaxValue)]
		[Display(Name = "Stop (points)", Description = "Fixed stop distance. 0 = use the average range", Order = 4, GroupName = "3. Trade")]
		public double StopPoints { get; set; }

		[NinjaScriptProperty]
		[Range(0, 1000)]
		[Display(Name = "Cost per contract ($)", Description = "Round-turn commission plus one tick of slippage", Order = 5, GroupName = "3. Trade")]
		public double CostPerContract { get; set; }

		[NinjaScriptProperty]
		[Range(1, 20)]
		[Display(Name = "Max trades per day", Order = 6, GroupName = "3. Trade")]
		public int MaxTradesPerDay { get; set; }

		[NinjaScriptProperty]
		[Display(Name = "Entry", Order = 1, GroupName = "4. Entry")]
		public BoldHarvestEntry Entry { get; set; }

		[NinjaScriptProperty]
		[Range(1, 120)]
		[Display(Name = "Opening range (minutes)", Order = 2, GroupName = "4. Entry")]
		public int OpeningRangeMinutes { get; set; }

		[NinjaScriptProperty]
		[Range(1, 120)]
		[Display(Name = "First candle (minutes)", Order = 3, GroupName = "4. Entry")]
		public int FirstCandleMinutes { get; set; }

		[NinjaScriptProperty]
		[Range(0, 235959)]
		[Display(Name = "Session start (HHmmss)", Order = 1, GroupName = "5. Session")]
		public int SessionStartTime { get; set; }

		[NinjaScriptProperty]
		[Range(0, 235959)]
		[Display(Name = "Session end (HHmmss)", Order = 2, GroupName = "5. Session")]
		public int SessionEndTime { get; set; }

		[NinjaScriptProperty]
		[Range(0, 235959)]
		[Display(Name = "Last entry (HHmmss)", Order = 3, GroupName = "5. Session")]
		public int LastEntryTime { get; set; }

		[NinjaScriptProperty]
		[Range(0, 235959)]
		[Display(Name = "Flat exit (HHmmss)", Order = 4, GroupName = "5. Session")]
		public int FlatExitTime { get; set; }
		#endregion

		protected override void OnStateChange()
		{
			if (State == State.SetDefaults)
			{
				Description									= "Bold + Harvest: the account sets the bracket, the entry only picks the direction.";
				Name										= "BoldHarvest";
				Calculate									= Calculate.OnBarClose;
				EntriesPerDirection							= 1;
				EntryHandling								= EntryHandling.AllEntries;
				IsExitOnSessionCloseStrategy				= true;
				ExitOnSessionCloseSeconds					= 30;
				BarsRequiredToTrade							= 20;
				IncludeCommission							= true;
				StartBehavior								= StartBehavior.WaitUntilFlat;
				TimeInForce									= TimeInForce.Day;
				IsFillLimitOnTouch							= false;
				IsUnmanaged									= false;
				IsInstantiatedOnEachOptimizationIteration	= false;

				Phase					= BoldHarvestPhase.Evaluation;
				StartingBalance			= 50000;
				BalanceNow				= 0;
				FloorNow				= 0;
				DrawdownDollars			= 2000;
				DrawdownType			= BoldHarvestDrawdown.EndOfDayTrailing;
				DrawdownLocks			= true;
				LockOffsetDollars		= 100;
				DailyLossLimit			= 0;
				MaxContracts			= 5;

				GoalDollars				= 3000;
				MaxDayProfit			= 0;
				CoastDollars			= 0;

				MaxRiskFraction			= 1.0;
				MinOdds					= 1.0;
				StopRangeMultiple		= 0.25;
				StopPoints				= 0;
				CostPerContract			= 9.50;
				MaxTradesPerDay			= 2;

				Entry					= BoldHarvestEntry.NoiseBreakout;
				OpeningRangeMinutes		= 15;
				FirstCandleMinutes		= 5;

				SessionStartTime		= 93000;
				SessionEndTime			= 160000;
				LastEntryTime			= 153000;
				FlatExitTime			= 155900;
			}
			else if (State == State.DataLoaded)
			{
				ranges		= new double[Lookback];
				rangeCount	= 0;
				moves		= new double[Lookback][];
				moveCount	= 0;
				todayMoves	= NewMoves();
				checkTimes	= new int[Checks];
				for (int i = 0; i < Checks; i++)
					checkTimes[i] = AddMinutesToTime(SessionStartTime, 30 * (i + 1));

				orEndTime			= AddMinutesToTime(SessionStartTime, OpeningRangeMinutes);
				firstCandleEndTime	= AddMinutesToTime(SessionStartTime, FirstCandleMinutes);
				baselinePnl			= 0;
				lastDay				= 0;
				prevClose			= 0;
				ResetAccount();
				ResetSession();

				if (BarsPeriod.BarsPeriodType != BarsPeriodType.Minute || BarsPeriod.Value != 1)
					Print("BoldHarvest: built for 1-minute bars. Current series is "
						+ BarsPeriod.Value + " " + BarsPeriod.BarsPeriodType + ".");
			}
			else if (State == State.Realtime)
			{
				// Forget the simulated history: start from the real account.
				baselinePnl = ClosedPnl;
				ResetAccount();
				dayStartBalance = Balance;
				Print("BoldHarvest live: balance " + Balance.ToString("N0") + ", max loss level "
					+ floor.ToString("N0") + ", goal " + GoalBalance.ToString("N0"));
			}
		}

		protected override void OnBarUpdate()
		{
			if (BarsInProgress != 0 || CurrentBar < BarsRequiredToTrade)
				return;

			int t	= ToTime(Time[0]);
			int day	= ToDay(Time[0]);

			if (day != lastDay)
			{
				if (lastDay != 0)
					CloseDay();
				lastDay = day;
				ResetSession();
				dayStartBalance	= Balance;
				tradesToday		= 0;
				tradedThisDay	= false;
			}

			bool inSession = t > SessionStartTime && t <= SessionEndTime;
			if (inSession)
				TrackSession(t);

			if (Position.MarketPosition != MarketPosition.Flat)
				ManageOpenTrade();
			else if (!breached && Balance <= floor)
				breached = true;

			// ---- flat by the cutoff, whatever happened -------------------------------
			if (t >= FlatExitTime)
			{
				if (Position.MarketPosition == MarketPosition.Long)
					ExitLong("TimeExit", "BH Long");
				else if (Position.MarketPosition == MarketPosition.Short)
					ExitShort("TimeExit", "BH Short");
				return;
			}

			// ---- entry ---------------------------------------------------------------
			if (!inSession || t > LastEntryTime || breached)			return;
			if (Position.MarketPosition != MarketPosition.Flat)			return;
			if (tradesToday >= MaxTradesPerDay)							return;

			int direction = Signal(t);
			if (direction == 0)
				return;

			int qty, stopTicks, targetTicks;
			if (!PlanTrade(t, out qty, out stopTicks, out targetTicks))
				return;

			SetStopLoss(CalculationMode.Ticks, stopTicks);
			SetProfitTarget(CalculationMode.Ticks, targetTicks);
			stopPrice = 0;

			if (direction > 0)
				EnterLong(qty, "BH Long");
			else
				EnterShort(qty, "BH Short");

			tradesToday++;
			if (!tradedThisDay)
			{
				tradedThisDay = true;
				tradingDays++;
			}
		}

		#region Account
		private double ClosedPnl
		{
			get
			{
				return SystemPerformance.AllTrades.Count > 0
					? SystemPerformance.AllTrades.TradesPerformance.Currency.CumProfit
					: 0.0;
			}
		}

		/// <summary>Closed balance: the configured balance plus closed trades since it was set.</summary>
		private double Balance
		{
			get { return (BalanceNow > 0 ? BalanceNow : StartingBalance) + ClosedPnl - baselinePnl; }
		}

		private double GoalBalance
		{
			get { return StartingBalance + GoalDollars; }
		}

		private double LockLevel
		{
			get { return DrawdownLocks ? StartingBalance + LockOffsetDollars : double.MaxValue; }
		}

		private void ResetAccount()
		{
			double bal = BalanceNow > 0 ? BalanceNow : StartingBalance;
			floor			= FloorNow > 0 ? FloorNow : Math.Min(StartingBalance - DrawdownDollars, LockLevel);
			hwm				= bal;
			breached		= false;
			dayStartBalance	= bal;
			tradesToday		= 0;
			tradingDays		= 0;
			tradedThisDay	= false;
		}

		private void RaiseFloor(double equityHigh)
		{
			if (DrawdownType == BoldHarvestDrawdown.Static)
				return;
			hwm		= Math.Max(hwm, equityHigh);
			floor	= Math.Max(floor, Math.Min(hwm - DrawdownDollars, LockLevel));
		}

		/// <summary>Yesterday is final: roll the end-of-day drawdown and the histories.</summary>
		private void CloseDay()
		{
			double bal = Balance;
			if (DrawdownType == BoldHarvestDrawdown.EndOfDayTrailing)
				RaiseFloor(bal);
			if (bal <= floor)
				breached = true;

			if (!haveOpen)
				return;
			prevClose = lastRthClose;
			ranges[rangeCount % Lookback] = dayHigh - dayLow;
			rangeCount++;
			moves[moveCount % Lookback] = todayMoves;
			moveCount++;
			todayMoves = NewMoves();
		}

		/// <summary>The bracket the account calls for right now. False if there is nothing to do.</summary>
		private bool PlanTrade(int t, out int qty, out int stopTicks, out int targetTicks)
		{
			qty = 0; stopTicks = 0; targetTicks = 0;

			double bal		= Balance;
			double cushion	= bal - floor;
			double dayPnl	= bal - dayStartBalance;
			double need		= GoalBalance - bal;
			double risk;

			if (need > 0)
			{
				if (MaxDayProfit > 0)
					need = Math.Min(need, MaxDayProfit - dayPnl);
				if (need <= 1)
					return false;
				risk = Math.Min(cushion * MaxRiskFraction, need / MinOdds);
			}
			else
			{
				// Goal reached: at most one small trade a day, if asked for.
				if (CoastDollars <= 0 || tradesToday > 0)
					return false;
				need = CoastDollars;
				risk = Math.Min(CoastDollars, cushion * MaxRiskFraction);
			}

			if (DailyLossLimit > 0)
				risk = Math.Min(risk, DailyLossLimit + dayPnl);
			if (risk <= 0)
				return false;

			double stopDistance = StopDistance(t);
			if (stopDistance <= 0)
				return false;

			double pointValue = Instrument.MasterInstrument.PointValue;
			qty = (int)Math.Floor(risk / (stopDistance * pointValue) + 0.5);
			qty = Math.Max(1, Math.Min(qty, MaxContracts));

			double perTick	= pointValue * TickSize * qty;
			double cost		= qty * CostPerContract;
			if (perTick <= 0 || risk <= cost)
				return false;

			stopTicks	= Math.Max(1, (int)Math.Floor((risk - cost) / perTick));
			targetTicks	= Math.Max(1, (int)Math.Ceiling((need + cost) / perTick));
			return true;
		}

		/// <summary>Stop distance in points: a share of the average range, narrower late in the day.</summary>
		private double StopDistance(int t)
		{
			if (StopPoints > 0)
				return StopPoints;
			int n = Math.Min(rangeCount, Lookback);
			if (n < Lookback / 2)
				return 0;
			double sum = 0;
			for (int i = 0; i < n; i++)
				sum += ranges[i];
			double left = Math.Max(15, MinutesBetween(t, SessionEndTime));
			return StopRangeMultiple * (sum / n) * Math.Sqrt(Math.Min(1.0, left / 390.0));
		}

		/// <summary>Follow the floor while a trade is open, and get out if it is reached.</summary>
		private void ManageOpenTrade()
		{
			bool isLong			= Position.MarketPosition == MarketPosition.Long;
			double perPoint		= Position.Quantity * Instrument.MasterInstrument.PointValue;
			double openPnl		= Position.GetUnrealizedProfitLoss(PerformanceUnit.Currency, Close[0]);
			double bestPnl		= Position.GetUnrealizedProfitLoss(PerformanceUnit.Currency, isLong ? High[0] : Low[0]);

			if (DrawdownType == BoldHarvestDrawdown.IntradayTrailing)
				RaiseFloor(Balance + bestPnl);

			double room = Balance + openPnl - floor;
			if (room <= 0)
			{
				breached = true;
				if (isLong)	ExitLong("FloorExit", "BH Long");
				else		ExitShort("FloorExit", "BH Short");
				return;
			}

			if (DrawdownType != BoldHarvestDrawdown.IntradayTrailing || perPoint <= 0)
				return;

			// The price at which equity meets the floor: never leave the stop beyond it.
			double floorPrice = Instrument.MasterInstrument.RoundToTickSize(
				isLong ? Close[0] - room / perPoint + TickSize : Close[0] + room / perPoint - TickSize);
			bool tighter = stopPrice == 0
				|| (isLong && floorPrice > stopPrice)
				|| (!isLong && floorPrice < stopPrice);
			if (tighter && ((isLong && floorPrice < Close[0]) || (!isLong && floorPrice > Close[0])))
			{
				stopPrice = floorPrice;
				SetStopLoss(CalculationMode.Price, stopPrice);
			}
		}
		#endregion

		#region Session and signals
		private void ResetSession()
		{
			haveOpen		= false;
			haveFirstCandle	= false;
			orHigh			= double.MinValue;
			orLow			= double.MaxValue;
		}

		private double[] NewMoves()
		{
			double[] m = new double[Checks];
			for (int i = 0; i < Checks; i++)
				m[i] = double.NaN;
			return m;
		}

		private void TrackSession(int t)
		{
			if (!haveOpen)
			{
				haveOpen	= true;
				sessionOpen	= Open[0];
				dayHigh		= High[0];
				dayLow		= Low[0];
			}
			dayHigh			= Math.Max(dayHigh, High[0]);
			dayLow			= Math.Min(dayLow, Low[0]);
			lastRthClose	= Close[0];

			if (t <= orEndTime)
			{
				orHigh = Math.Max(orHigh, High[0]);
				orLow  = Math.Min(orLow,  Low[0]);
			}
			if (!haveFirstCandle && t >= firstCandleEndTime)
			{
				haveFirstCandle		= true;
				firstCandleClose	= Close[0];
			}
			for (int i = 0; i < Checks; i++)
				if (t == checkTimes[i] && sessionOpen > 0)
					todayMoves[i] = Math.Abs(Close[0] / sessionOpen - 1.0);
		}

		/// <summary>+1 to buy, -1 to sell, 0 for nothing, read from the bar that just closed.</summary>
		private int Signal(int t)
		{
			if (!haveOpen)
				return 0;

			switch (Entry)
			{
				case BoldHarvestEntry.AlwaysLong:
					return 1;

				case BoldHarvestEntry.AlwaysShort:
					return -1;

				case BoldHarvestEntry.GapFade:
					if (prevClose <= 0 || sessionOpen == prevClose)
						return 0;
					return sessionOpen > prevClose ? -1 : 1;

				case BoldHarvestEntry.FirstCandle:
					if (!haveFirstCandle || firstCandleClose == sessionOpen)
						return 0;
					return firstCandleClose > sessionOpen ? 1 : -1;

				case BoldHarvestEntry.OpeningRange:
					if (t <= orEndTime || orHigh == double.MinValue)
						return 0;
					if (Close[0] > orHigh)	return 1;
					if (Close[0] < orLow)	return -1;
					return 0;

				case BoldHarvestEntry.NoiseBreakout:
					return NoiseSignal(t);
			}
			return 0;
		}

		/// <summary>
		/// The noise area: the average move from the open by this time of day over the last
		/// 14 sessions, around the open and the previous close. Checked on the half hour.
		/// </summary>
		private int NoiseSignal(int t)
		{
			if (prevClose <= 0)
				return 0;
			int check = -1;
			for (int i = 0; i < Checks; i++)
				if (t == checkTimes[i])
					check = i;
			if (check < 0)
				return 0;

			int rows = Math.Min(moveCount, Lookback);
			double sum = 0;
			int n = 0;
			for (int r = 0; r < rows; r++)
			{
				double m = moves[r][check];
				if (!double.IsNaN(m))
				{
					sum += m;
					n++;
				}
			}
			if (n < Lookback / 2)
				return 0;

			double sigma = sum / n;
			double upper = Math.Max(sessionOpen, prevClose) * (1.0 + sigma);
			double lower = Math.Min(sessionOpen, prevClose) * (1.0 - sigma);
			if (Close[0] > upper)	return 1;
			if (Close[0] < lower)	return -1;
			return 0;
		}
		#endregion

		#region Time helpers
		private static int AddMinutesToTime(int hhmmss, int minutes)
		{
			int h = hhmmss / 10000;
			int m = (hhmmss / 100) % 100;
			int s = hhmmss % 100;
			DateTime d = new DateTime(2000, 1, 1, h, m, s).AddMinutes(minutes);
			return d.Hour * 10000 + d.Minute * 100 + d.Second;
		}

		private static double MinutesBetween(int fromHhmmss, int toHhmmss)
		{
			int from = (fromHhmmss / 10000) * 60 + (fromHhmmss / 100) % 100;
			int to   = (toHhmmss / 10000) * 60 + (toHhmmss / 100) % 100;
			return to - from;
		}
		#endregion
	}
}
