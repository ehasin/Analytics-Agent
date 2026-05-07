discovery = [
    {
        "question": "Describe the data you have access to.",
        "validate": lambda ans: (
            sum(1 for t in ["orders", "order_items", "products", "customers", "sellers", "payments", "reviews"]
                if t.replace("_", " ") in ans.lower() or t in ans.lower()) >= 4
            and any(x in ans.lower() for x in ["join", "relat", "connect", "link", "foreign key", "order_id"])
        ),
        "expected": "All 7 table names with descriptions, key KPIs, key business dimensions, row counts, and a conceptual explanation of how tables connect (orders as central hub)"
    },
    {
        "question": "What tables are available and how do they relate to each other?",
        "validate": lambda ans: (
            sum(1 for t in ["orders", "order_items", "products", "customers", "sellers", "payments", "reviews"]
                if t.replace("_", " ") in ans.lower() or t in ans.lower()) >= 4
            and any(x in ans.lower() for x in ["join", "relat", "connect", "link", "foreign key", "order_id"])
        ),
        "expected": "All 7 table names listed with join relationships: orders as hub, order_items linking to products/sellers, payments/reviews linking to orders, customers linking to orders"
    },
    {
        "question": "Describe all the data Google indexes.",
        "validate": lambda ans: any(x in ans.lower() for x in ["can't answer", "cannot answer", "not available", "only", "this data", "one company", "single", "does not contain", "doesn't contain", "unable", "no data"]),
        "expected": "Should decline: out of scope. Data only covers one specific company, not all Google-indexed data worldwide"
    },
    {
        "question": "What questions can you answer with this data?",
        "validate": lambda ans: (
            sum(1 for x in ["revenue", "delivery", "satisfaction", "review", "payment", "geographic", "category", "seller", "trend"]
                if x in ans.lower()) >= 3
            and len(ans.strip()) > 100
        ),
        "expected": "A summary of analytical capabilities: revenue/order trends, delivery performance, customer satisfaction, payment analysis, geographic patterns, category/seller comparisons. Should also mention limitations (no cost/margin, no inventory, no marketing data)"
    },
    {
        "question": "What can't you answer? What are the limitations of the available data?",
        "validate": lambda ans: (
            sum(1 for x in ["cost", "profit", "margin", "inventory", "stock", "marketing", "channel",
                            "demographic", "real-time", "beyond 2018", "after 2018", "identity"]
                if x in ans.lower()) >= 2
            and len(ans.strip()) > 50
        ),
        "expected": "Explicit limitations: no cost/COGS/margin data, no inventory, no marketing attribution, no customer demographics, anonymized IDs, data limited to 2016-2018, single marketplace (Olist), all values in BRL"
    },
    {
        "question": "How is revenue defined in your data model? What fields should I use to calculate it?",
        "validate": lambda ans: (
            any(x in ans.lower() for x in ["price", "freight"])
            and any(x in ans.lower() for x in ["order_items", "order items"])
            and any(x in ans.lower() for x in ["cancel", "status"])
        ),
        "expected": "Gross Revenue = SUM(price + freight_value) from order_items, Net Revenue = SUM(Price) from order_items joined to orders with order_status != 'canceled'. Should mention that payments.payment_value is an alternative but may not match exactly due to vouchers/rounding"
    },
]

easy = [
    {
        "question": "What is the average item price across all orders?",
        "validate": lambda ans: any(x in ans for x in ["120", "121", "160", "161"]),
        "expected": "~120 per item or ~160 per order (both valid interpretations)"
    },
    {
        "question": "Which city has the most customers?",
        "validate": lambda ans: "sao paulo" in ans.lower(),
        "expected": "Sao Paulo"
    },
    {
        "question": "What is the average review score?",
        "validate": lambda ans: "4.0" in ans or "4.1" in ans,
        "expected": "~4.0-4.1 average score"
    },
    {
        "question": "What percentage of orders paid by credit card were paid by credit card?",
        "validate": lambda ans: any(x in ans.lower() for x in ["clarif", "please", "trivial", "100%", "by definition", "tautolog", "always"]),
        "expected": "Should flag as trivially meaningless — answer is always 100% by definition"
    },
    {
        "question": "How many orders were delivered late (delivered after estimated delivery date)?",
        "validate": lambda ans: any(char.isdigit() for char in ans),
        "expected": "A numeric count of late deliveries"
    },
    {
        "question": "What is the total revenue by payment type?",
        "validate": lambda ans: "credit_card" in ans.lower(),
        "expected": "Breakdown including credit_card as top type"
    },
    {
        "question": "Which seller has the most orders?",
        "validate": lambda ans: len(ans.strip()) > 0,
        "expected": "A seller ID"
    },
    {
        "question": "What is the month with the highest number of orders?",
        "validate": lambda ans: any(char.isdigit() for char in ans),
        "expected": "A month (number or name)"
    },
    {
        "question": "What is the average freight cost per item across all ecommerce companies worldwide?",
        "validate": lambda ans: any(x in ans.lower() for x in ["can't answer", "cannot answer", "not available", "only", "this data", "one company", "single", "does not contain", "doesn't contain", "unable", "no data"]),
        "expected": "Should decline: data only covers one company, not all ecommerce worldwide"
    },
    {
        "question": "What's the best ever?",
        "validate": lambda ans: any(x in ans.lower() for x in ["clarif", "unclear", "specify", "which", "what do you mean", "ambiguous", "please provide", "what metric", "define"]),
        "expected": "Should ask for clarification: best by which metric? which entity?"
    },
]

hard = [
    {
        "question": "What is the average delivery time in days for each customer state, only for delivered orders?",
        "validate": lambda ans: "SP" in ans and any(char.isdigit() for char in ans),
        "expected": "A breakdown by state with SP included, showing average days"
    },
    {
        "question": "Which product category has the highest percentage of 1-star reviews?",
        "validate": lambda ans: any(char.isalpha() for char in ans) and ("%" in ans or "." in ans),
        "expected": "A category name with a percentage or ratio"
    },
    {
        "question": "What is the repeat purchase rate? That is, what percentage of unique customers placed more than one order?",
        "validate": lambda ans: "%" in ans or "." in ans,
        "expected": "A low percentage (~3%) since most customers order once"
    },
    {
        "question": "For orders paid by credit card, what is the average number of installments and how does average order value differ by installment count?",
        "validate": lambda ans: "installment" in ans.lower() or any(str(i) in ans for i in range(1, 13)),
        "expected": "A table or breakdown showing installment counts with corresponding avg order values"
    },
    {
        "question": "Which sellers have an average review score below 2.0 and more than 10 orders? List them with their city and order count.",
        "validate": lambda ans: "seller" in ans.lower() or len(ans.strip()) > 50,
        "expected": "A list of seller IDs with city and order count, or a statement that none qualify"
    },
    {
        "question": "What is the month-over-month order growth rate for 2017?",
        "validate": lambda ans: "2017" in ans or ("%" in ans and any(char.isdigit() for char in ans)),
        "expected": "Monthly growth rates as percentages for 2017"
    },
    {
        "question": "Is there a correlation between product weight and freight cost? What is the Pearson correlation coefficient?",
        "validate": lambda ans: "0." in ans or "corr" in ans.lower(),
        "expected": "A correlation coefficient between 0 and 1"
    },
    {
        "question": "What are the top 5 seller-customer state pairs by order volume? For example, seller in SP shipping to RJ.",
        "validate": lambda ans: "SP" in ans and len(ans.strip()) > 30,
        "expected": "Pairs like SP->SP, SP->RJ etc. with counts"
    },
    {
        "question": "What is the average profit margin per category, assuming COGS is 60% of the item price?",
        "validate": lambda ans: any(x in ans.lower() for x in ["can't answer", "cannot answer", "not available", "assumption", "no cost", "no cogs"]) or ("%" in ans and any(char.isalpha() for char in ans)),
        "expected": "Declines (no real COGS data), the question leads to meaningless analysis."
    },
    {
        "question": "Compare the delivery performance of the top 3 carriers by volume: what percentage of their orders arrived late?",
        "validate": lambda ans: any(x in ans.lower() for x in ["can't answer", "cannot answer", "no carrier", "not available", "does not contain"]),
        "expected": "Should decline: the dataset has no carrier information"
    },
]

misleading = [
    # Judgment calls - needs clarification
    {
        "question": "Who is our best customer?",
        "validate": lambda ans: any(x in ans.lower() for x in ["clarif", "unclear", "specify", "which", "metric", "define", "what do you mean", "criteria"]),
        "expected": "clarifications_needed: best by what? revenue, order count, review score?"
    },
    {
        "question": "Which sellers should we drop from the platform?",
        "validate": lambda ans: any(x in ans.lower() for x in ["clarif", "unclear", "specify", "criteria", "define", "what", "threshold", "based on"]),
        "expected": "clarifications_needed: drop based on what criteria? low reviews, late delivery, low volume?"
    },
    {
        "question": "Is our business doing well?",
        "validate": lambda ans: any(x in ans.lower() for x in ["clarif", "unclear", "specify", "define", "what", "metric", "compared"]),
        "expected": "clarifications_needed: well compared to what? which KPI?"
    },

    # Speculation with partial data - has local data but needs external reference
    {
        "question": "How does the average order value on our platform compare to the average ecommerce order value in Brazil?",
        "validate": lambda ans: any(x in ans.lower() for x in ["external", "outside", "can't answer", "cannot answer", "not available", "only", "no data", "benchmark", "don't have"]),
        "expected": "Can calculate our AOV but should flag that Brazil-wide benchmark is not in the dataset"
    },
    {
        "question": "Are our customers in São Paulo wealthier than the national average?",
        "validate": lambda ans: any(x in ans.lower() for x in ["can't answer", "cannot answer", "no income", "no wealth", "not available", "no data", "demographic", "missing"]),
        "expected": "cant_answer: no income or wealth data in the dataset at all"
    },

    # Wrong table / wrong source
    {
        "question": "Based on the customers table, which customer segment is most profitable?",
        "validate": lambda ans: any(x in ans.lower() for x in ["can't answer", "cannot answer", "no segment", "no profit", "not available", "no cost", "missing", "does not contain"]),
        "expected": "cant_answer: customers table has no segment or profitability fields"
    },
    {
        "question": "What is the return rate by product category?",
        "validate": lambda ans: any(x in ans.lower() for x in ["can't answer", "cannot answer", "no return", "not available", "missing", "does not contain"]),
        "expected": "cant_answer: no returns data in the dataset"
    },

    # Misleading / sounds answerable but isn't
    {
        "question": "What is the conversion rate from cart to purchase by product category?",
        "validate": lambda ans: any(x in ans.lower() for x in ["can't answer", "cannot answer", "no cart", "no funnel", "not available", "missing", "no browsing", "no event"]),
        "expected": "cant_answer: no cart or browsing funnel data, only completed orders"
    },
    {
        "question": "What is the customer lifetime value for the top 10 customers?",
        "validate": lambda ans: any(x in ans.lower() for x in ["can't answer", "cannot answer", "clarif", "lifetime", "define", "assumption", "limited"]) or any(char.isdigit() for char in ans),
        "expected": "Either clarifications_needed (LTV definition varies) or attempts an answer with caveats (limited repeat purchase data)"
    },

    # Completely off-domain
    {
        "question": "What was the weather in São Paulo on the day with the most orders?",
        "validate": lambda ans: any(x in ans.lower() for x in ["can't answer", "cannot answer", "no weather", "not available", "external", "outside", "missing"]),
        "expected": "cant_answer: no weather data in the dataset"
    },
]

multistage = [
    # Stage 1+2: Product size classification → sales comparison
    {
        "question": "First, classify products into 'small', 'medium', and 'bulky' based on the 33rd and 66th percentiles of product_weight_g. Then show total revenue and average review score for each size class.",
        "validate": lambda ans: all(x in ans.lower() for x in ["small", "medium", "bulky"]) and any(char.isdigit() for char in ans),
        "expected": "Three size classes with revenue and avg review score for each"
    },

    # Stage 1+2+3: Best month → top categories → consistency
    {
        "question": "Find the month with the highest total revenue. Then identify the top 3 product categories in that month. Finally, show how those same 3 categories performed in every other month of the same year — were they consistently top sellers or just spiking in that one month?",
        "validate": lambda ans: (
            ("november" in ans.lower() or "nov" in ans.lower() or "2017-11" in ans)
            and "2017" in ans
            and (
                sum(m in ans for m in ["2017-01","2017-02","2017-03","2017-04","2017-05","2017-06","2017-07","2017-08","2017-09","2017-10","2017-12"]) >= 6
                or sum(m in ans.lower() for m in ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","dec"]) >= 6
            )
        ),
        "expected": "A month identified, top 3 categories named, then a monthly breakdown of those categories across the year"
    },

    # Stage 1+2: Customer segmentation → behavior comparison
    {
        "question": "Segment customers into 'one-time' and 'repeat' based on whether customer_unique_id appears in more than one order. Then compare these two segments on: average order value, average review score, and average delivery time in days.",
        "validate": lambda ans: all(x in ans.lower() for x in ["one-time", "repeat"]) or ("one" in ans.lower() and "repeat" in ans.lower()),
        "expected": "Two segments with three metrics compared side by side"
    },
    # Stage 1+2: Seller tier → late delivery analysis
    {
        "question": "Rank sellers into 'high volume' (top 10% by order count), 'medium volume' (next 40%), and 'low volume' (bottom 50%). Then calculate the percentage of late deliveries for each seller tier.",
        "validate": lambda ans: ("high" in ans.lower() or "top" in ans.lower()) and ("%" in ans or "late" in ans.lower()),
        "expected": "Three seller tiers with late delivery percentages"
    },

    # Stage 1+2: Geographic distance proxy → delivery impact
    {
        "question": "Classify orders as 'same state' or 'cross state' based on whether the seller state matches the customer state. Then compare average delivery time, average freight cost, and average review score between the two groups.",
        "validate": lambda ans: ("same" in ans.lower() and "cross" in ans.lower()) and any(char.isdigit() for char in ans),
        "expected": "Two groups with delivery time, freight cost, and review score compared"
    },
]

realistic = [
    {
        "question": "How many unique customers are there? Of these, how many in our top-selling city (Gross Revenue)? Was this city always our top-selling one? If not, since when?",
        "validate": lambda ans: "96096" in ans.replace(",", "").replace(" ", "") or "96,096" in ans,
        "expected": "96,096 unique customers, São Paulo as top city with customer count, and historical city ranking"
    },
    {
        "question": "Tell me about our company. What are we selling? Typical products, prices, locations.",
        "validate": lambda ans: any(char.isdigit() for char in ans) and any(x in ans.lower() for x in ["categor", "product", "price", "city", "state"]),
        "expected": "Overview of product categories, price ranges, and geographic footprint"
    },
    {
        "question": "In which product categories do we struggle (Gross Revenue, Customer Satisfaction, other meaningful KPIs)? Try to explain potential underlying reasons.",
        "validate": lambda ans: any(char.isalpha() for char in ans) and len(ans.strip()) > 100,
        "expected": "Low-performing categories identified with revenue, review scores, and possible explanations"
    },
    {
        "question": "Analyze the correlation between product weight, shipping costs, and customer satisfaction to identify logistics optimization opportunities.",
        "validate": lambda ans: ("corr" in ans.lower() or ans.count("0.") >= 2) and 
            any(x in ans.lower() for x in ["weight", "freight"]) and 
            any(x in ans.lower() for x in ["review", "score", "satisfaction"]),
        "expected": "Correlation coefficients between weight/freight/reviews with optimization insights"
    },
    {
        "question": "Which seller demonstrates highest rate of customer repeat-sales (net revenue, multiple purchases per customer from the same seller)?. Exclude cases with <10 customers.",
        "validate": lambda ans: any(char.isalnum() for char in ans) and ("seller" in ans.lower() or len(ans.strip()) > 30),
        "expected": "A seller ID with repeat purchase rate, filtered by minimum 10 customers, using non-cancelled orders"
    },
    {
        "question": "What is our cost structure? Do I pay commissions to suppliers, or is it cogs only? Something else?",
        "validate": lambda ans: any(x in ans.lower() for x in ["can't answer", "cannot answer", "no cost", "not available", "missing"]),
        "expected": "cant_answer: no cost structure, COGS, or commission data in the dataset"
    },
    {
        "question": "What are the listed use cases and main kpis for the payments table?",
        "validate": lambda ans: any(x in ans.lower() for x in ["payment", "revenue", "installment", "credit", "method"]),
        "expected": "Use cases and KPIs from the data model metadata, optionally enriched with actual data statistics"
    },
    {
        "question": "Do you see problematic geographic areas, where gross sales might be inhibited by lack of sufficient payment method options?",
        "validate": lambda ans: any(x in ans.lower() for x in ["state", "credit", "boleto", "payment", "geographic", "region"]) and any(char.isdigit() for char in ans),
        "expected": "Geographic breakdown of payment method diversity by state with gross sales, identification of states with limited payment options, and caveat on causal claims"
    },
    {
        "question": "What was our customer acquisition cost in 2017?",
        "validate": lambda ans: any(x in ans.lower() for x in ["can't answer", "cannot answer", "no cost", "not available", "missing", "no marketing", "no acquisition"]),
        "expected": "cant_answer: no marketing spend or acquisition cost data in the dataset"
    },
]