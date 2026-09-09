-- PostgreSQL / Beekeeper: run the entire statement. No data is changed.
-- Mirrors the compact games endpoint using CURRENT rules, not a historical snapshot.
-- Settings below must match GRIND_ALGO in the deployed backend.
-- Stored market taxonomy is preferred; fallback covers canonical daily markets.
WITH settings AS (
    SELECT
        (CURRENT_TIMESTAMP AT TIME ZONE 'Africa/Lagos')::date - 1 AS target_date,
        false AS publish_dc12,
        false AS publish_wild_cards,
        70.0 AS min_confidence, 0.03 AS min_ev,
        5.0 AS probation_confidence_extra, 0.03 AS probation_ev_extra,
        3.0 AS calibration_confidence_extra, 0.02 AS calibration_ev_extra
), daily_run AS (
    SELECT r.id
    FROM algo_algorun r CROSS JOIN settings s
    WHERE r.target_date = s.target_date AND r.status = 'success'
      AND COALESCE(r.result->>'publish_policy', '') <> 'on_demand_fixture_analysis'
    ORDER BY
        CASE WHEN r.result->>'publish_policy' IN
            ('strict_accuracy_gate', 'celery_fanout_pipeline') THEN 1 ELSE 0 END DESC,
        (SELECT COUNT(*) FROM algo_algofixture f WHERE f.run_id = r.id) DESC,
        r.created_at DESC
    LIMIT 1
), unpaused AS (
    SELECT p.*,
        COALESCE(NULLIF(p.insights->'council_review', 'null'::jsonb), '{}'::jsonb) AS review,
        COALESCE(NULLIF(p.insights #>> '{market_taxonomy,family}', ''),
                 NULLIF(p.insights->>'market_family', ''),
            CASE
                WHEN p.market ~* 'BTTS|both teams' THEN 'btts'
                WHEN p.market ~* '^DC:' THEN 'double_chance'
                WHEN p.market ~* '^DNB|draw no bet' THEN 'draw_no_bet'
                WHEN p.market ~* '^(Home Win|Away Win|Draw)$' THEN 'match_result'
                WHEN p.market ~* 'corner' THEN
                    CASE WHEN p.market ~* 'home|away' THEN 'team_corners' ELSE 'corners_total' END
                WHEN p.market ~* 'booking point' THEN 'booking_points'
                WHEN p.market ~* 'card' THEN
                    CASE WHEN p.market ~* 'home|away' THEN 'team_cards' ELSE 'cards_total' END
                WHEN p.market ~* 'shots? on target' THEN
                    CASE WHEN p.market ~* 'home|away|team' THEN 'team_shots_on_target'
                         ELSE 'shots_on_target_total' END
                WHEN p.market ~* '^(Over|Under) [0-9]' THEN 'total_goals'
                WHEN p.market ~* '^(1H|2H) (Over|Under) [0-9]' THEN 'total_goals'
                WHEN p.market ~* '(home|away|team).*(over|under)' THEN 'team_total_goals'
                ELSE 'unknown'
            END) AS family
    FROM algo_marketprediction p
    JOIN daily_run r ON r.id = p.run_id
    CROSS JOIN settings s
    WHERE p.match_id <> ''
      AND p.market NOT IN ('DC: 1X', 'DC: X2')
      AND (s.publish_dc12 OR p.market <> 'DC: 12')
      AND NOT ((' ' || LOWER(p.market) || ' ') LIKE '% under %')
      AND COALESCE(p.insights #>> '{market_taxonomy,side}', '') <> 'under'
      AND COALESCE(p.insights #>> '{market_taxonomy,selection}', '') <> 'under'
), visible_candidates AS (
    SELECT p.*
    FROM unpaused p
    WHERE EXISTS (
        SELECT 1 FROM algo_algofixture f
        WHERE f.run_id = p.run_id AND f.match_id = p.match_id
    )
      AND EXISTS (
        SELECT 1 FROM unpaused e
        WHERE e.run_id = p.run_id AND e.match_id = p.match_id AND e.eligible
    )
      AND COALESCE((p.insights->>'analysis_available')::boolean, p.eligible)
), review_values AS (
    SELECT p.*,
        CASE WHEN review = '{}'::jsonb THEN 'not_reviewed'
             ELSE COALESCE(review->>'decision', '') END AS decision,
        COALESCE(review->>'tier', '') AS council_tier,
        COALESCE(NULLIF((review->>'final_confidence')::numeric, 0), confidence) AS final_confidence,
        COALESCE((review->>'disagreement_score')::numeric, 0) AS disagreement,
        COALESCE((SELECT (v->>'score')::numeric
            FROM jsonb_array_elements(COALESCE(NULLIF(review->'reviewers', 'null'::jsonb), '[]')) v
            WHERE v->>'reviewer' = 'market_fit' LIMIT 1), 0) AS fit_raw,
        COALESCE((SELECT (v->>'score')::numeric
            FROM jsonb_array_elements(COALESCE(NULLIF(review->'reviewers', 'null'::jsonb), '[]')) v
            WHERE v->>'reviewer' = 'scoreline_pattern' LIMIT 1), 0) AS scoreline_raw,
        COALESCE((SELECT (v->>'score')::numeric
            FROM jsonb_array_elements(COALESCE(NULLIF(review->'reviewers', 'null'::jsonb), '[]')) v
            WHERE v->>'reviewer' = 'value' LIMIT 1), 0) AS value_raw,
        COALESCE(insights #>> '{league_trust,status}', '') AS league_trust,
        COALESCE(insights #>> '{calibration_trust,status}', '') AS calibration_trust
    FROM visible_candidates p
), assessed AS (
    SELECT p.*,
        COALESCE(NULLIF((review->>'consensus_score')::numeric, 0), final_confidence) AS consensus,
        CASE WHEN ev IS NULL THEN -12 ELSE GREATEST(-12, LEAST(14, ev * 35)) END AS ev_score,
        (
            (eligible OR (fit_raw >= 85 AND confidence >= s.min_confidence + 8 AND ev >= s.min_ev + 0.06))
            AND LOWER(odds_source) <> 'estimated'
            AND ev IS NOT NULL AND ev >= s.min_ev AND confidence >= s.min_confidence
            AND (confidence >= 70 OR s.publish_wild_cards)
            AND NOT EXISTS (
                SELECT 1 FROM jsonb_array_elements_text(risk_flags) flag
                WHERE flag IN (
                    'best_price_far_above_consensus', 'below_dc12_value_threshold',
                    'below_market_threshold', 'draw_boundary_risk', 'estimated_odds',
                    'goal_line_boundary', 'low_market_hit_rate', 'market_loss_streak',
                    'market_recent_losses', 'market_recent_low_hit_rate', 'market_suppressed',
                    'negative_market_roi', 'no_real_odds', 'strategy_suppressed',
                    'strategy_cooling', 'team_news_heavy_absences', 'thin_edge', 'wide_odds_market'
                ) AND NOT (fit_raw >= 85 AND flag IN (
                    'low_market_hit_rate', 'market_recent_low_hit_rate', 'market_suppressed',
                    'negative_market_roi', 'strategy_suppressed', 'strategy_cooling'
                ))
            )
            AND (league_trust <> 'restricted' OR
                (fit_raw >= 85 AND confidence >= s.min_confidence + 8 AND ev >= s.min_ev + 0.06))
            AND (league_trust <> 'probation' OR
                (confidence >= s.min_confidence + s.probation_confidence_extra AND ev >= s.min_ev + s.probation_ev_extra))
            AND calibration_trust <> 'restricted'
            AND (calibration_trust <> 'probation' OR
                (confidence >= s.min_confidence + s.calibration_confidence_extra AND ev >= s.min_ev + s.calibration_ev_extra))
        ) AS base_recommended
    FROM review_values p CROSS JOIN settings s
), gated AS (
    SELECT p.*,
        COALESCE(base_recommended, false) AND (
            decision = 'not_reviewed' OR
            (decision <> 'reject' AND council_tier NOT IN ('', 'watchlist')
             AND (council_tier <> 'wild_card' OR s.publish_wild_cards))
        ) AS recommended,
        CASE
            WHEN decision = 'not_reviewed' THEN
                CASE WHEN base_recommended THEN CASE WHEN confidence >= 80 THEN 'strong' ELSE 'playable' END
                     WHEN eligible AND confidence >= 60 AND ev > 0 THEN 'watchlist' ELSE 'no_edge' END
            WHEN decision = 'reject' OR council_tier = '' THEN
                CASE WHEN base_recommended OR (eligible AND confidence >= 60 AND ev > 0)
                     THEN 'watchlist' ELSE 'no_edge' END
            WHEN council_tier = 'wild_card' AND NOT s.publish_wild_cards THEN 'watchlist'
            WHEN NOT COALESCE(base_recommended, false) THEN
                CASE WHEN eligible AND confidence >= 60 AND ev > 0 THEN 'watchlist' ELSE 'no_edge' END
            WHEN council_tier = 'watchlist' THEN 'watchlist'
            WHEN decision = 'caution' THEN CASE WHEN council_tier = 'wild_card' THEN 'watchlist' ELSE 'recommended' END
            WHEN council_tier = 'banker' THEN 'strong'
            ELSE 'recommended'
        END AS recommendation_status,
        COALESCE(NULLIF(fit_raw, 0), consensus) AS market_fit,
        COALESCE(NULLIF(scoreline_raw, 0), consensus) AS scoreline_fit,
        COALESCE(NULLIF(value_raw, 0), consensus) AS value_score
    FROM assessed p CROSS JOIN settings s
), scores AS (
    SELECT p.*,
        CASE family
            WHEN 'total_goals' THEN 78 WHEN 'btts' THEN 74 WHEN 'corners_total' THEN 70
            WHEN 'match_result' THEN 66 WHEN 'draw_no_bet' THEN 62 WHEN 'double_chance' THEN 58
            WHEN 'team_total_goals' THEN 46 WHEN 'shots_on_target_total' THEN 42
            WHEN 'team_shots_on_target' THEN 36 WHEN 'team_corners' THEN 30
            WHEN 'cards_total' THEN 28 WHEN 'booking_points' THEN 24 WHEN 'team_cards' THEN 18 ELSE 35 END
        - CASE WHEN odds <> 0 AND odds <= 1.10 THEN 30 WHEN odds <> 0 AND odds <= 1.20 THEN 18
               WHEN odds <> 0 AND odds <= 1.30 THEN 8 ELSE 0 END
        - CASE recommendation_status WHEN 'no_edge' THEN 12 WHEN 'watchlist' THEN 6 ELSE 0 END
        - CASE WHEN EXISTS (SELECT 1 FROM jsonb_array_elements_text(risk_flags) flag
                            WHERE POSITION('using_league_average' IN flag) > 0) THEN 8 ELSE 0 END
        - CASE WHEN risk_flags ? 'referee_card_profile_missing' AND family IN ('cards_total', 'team_cards', 'booking_points') THEN 10 ELSE 0 END
        - CASE WHEN eligible THEN 0 ELSE 5 END AS headline_rank,
        COALESCE((insights->>'calibrated_probability')::numeric * 100,
                 (insights->>'raw_probability')::numeric * 100, final_confidence) AS model_probability,
        consensus * 0.34 + market_fit * 0.22 + scoreline_fit * 0.14 + final_confidence * 0.18
        + value_score * 0.10 + ev_score - disagreement * 0.45
        + CASE decision WHEN 'approve' THEN 10 WHEN 'caution' THEN 3 WHEN 'reject' THEN -70 ELSE 0 END
        - CASE WHEN market = 'DC: 12' THEN 8 ELSE 0 END
        - CASE WHEN market = 'DC: 12' AND (risk_flags ? 'draw_boundary_risk'
               OR POSITION('Draw pressure' IN COALESCE(insights->>'avoid_reason', '')) > 0) THEN 8 ELSE 0 END
        - COALESCE((SELECT SUM(penalty) FROM (VALUES
            ('thin_edge', 3), ('goal_line_boundary', 24), ('german_under_goals_market_blocked', 70),
            ('under25_goal_volatility', 20), ('under35_blowout_risk', 28), ('under45_high_goal_volatility', 18),
            ('corner_line_boundary', 18), ('corner_under_pressure_risk', 12), ('corner_over_margin_risk', 10),
            ('nordic_under_volatility', 16), ('best_price_far_above_consensus', 18), ('wide_odds_market', 12)
          ) penalties(flag, penalty) WHERE risk_flags ? flag), 0)
        - CASE WHEN risk_flags ?| ARRAY['market_suppressed', 'strategy_suppressed'] THEN 35 ELSE 0 END
        - CASE WHEN risk_flags ?| ARRAY['market_loss_streak', 'market_recent_losses'] THEN 22 ELSE 0 END
        - CASE WHEN risk_flags ?| ARRAY['market_cooling', 'strategy_cooling'] THEN 3 ELSE 0 END
        + CASE WHEN risk_flags ?| ARRAY['market_recovered', 'strategy_promoted'] THEN 3 ELSE 0 END
        - CASE WHEN eligible THEN 0 ELSE 12 END AS display_score
    FROM gated p
), ranked AS (
    SELECT p.*, ROW_NUMBER() OVER (
        PARTITION BY run_id, match_id
        ORDER BY published DESC, recommended DESC, headline_rank DESC,
            CASE recommendation_status WHEN 'strong' THEN 5 WHEN 'recommended' THEN 4
                WHEN 'playable' THEN 3 WHEN 'watchlist' THEN 2 WHEN 'no_edge' THEN 1 ELSE 0 END DESC,
            model_probability DESC,
            CASE LOWER(COALESCE(insights->>'data_quality', ''))
                WHEN 'calibrated' THEN 6 WHEN 'strong' THEN 5 WHEN 'fresh' THEN 5 WHEN 'medium' THEN 4
                WHEN 'limited' THEN 3 WHEN 'partial' THEN 3 WHEN 'poor' THEN 2
                WHEN 'unavailable' THEN 1 WHEN 'unknown' THEN 1 ELSE 0 END DESC,
            jsonb_array_length(risk_flags) ASC,
            CASE WHEN recommended THEN 4 ELSE CASE decision
                WHEN 'approve' THEN 3 WHEN 'caution' THEN 2 WHEN 'reject' THEN 0 ELSE 1 END END DESC,
            display_score DESC, final_confidence DESC, consensus DESC, market_fit DESC,
            scoreline_fit DESC, ev_score DESC, confidence DESC, odds ASC,
            ev DESC NULLS FIRST, market ASC, id ASC
    ) AS market_rank
    FROM scores p
)
SELECT run_id, match_date, match_id, fixture AS game, league,
       market AS top_market, final_confidence AS confidence, odds, score,
       CASE status WHEN 'win' THEN 'WIN' WHEN 'loss' THEN 'LOST'
                   WHEN 'void' THEN 'VOID' ELSE 'PENDING' END AS settlement,
       result AS settlement_details,
       settled_at AT TIME ZONE 'Africa/Lagos' AS settled_at_lagos,
       COUNT(*) OVER () AS total_visible_games
FROM ranked
WHERE market_rank = 1
ORDER BY league, kickoff, fixture;
