--
-- PostgreSQL database dump
--

--

-- Dumped from database version 16.15 (Debian 16.15-1.pgdg13+2)
-- Dumped by pg_dump version 16.15 (Debian 16.15-1.pgdg13+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: SCHEMA "public"; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA "public" IS 'standard public schema';


--
-- Name: matchup_set_implied(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION "public"."matchup_set_implied"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
    IF NEW.vegas_total IS NOT NULL AND NEW.vegas_spread IS NOT NULL THEN
        NEW.implied_points := (NEW.vegas_total / 2.0) - (NEW.vegas_spread / 2.0);
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = "heap";

--
-- Name: cfbd_player_overrides; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."cfbd_player_overrides" (
    "platform" "text" NOT NULL,
    "player_name" "text" NOT NULL,
    "pos" "text" NOT NULL,
    "cfbd_athlete_id" "text" NOT NULL
);


--
-- Name: derived_rankings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."derived_rankings" (
    "id" bigint NOT NULL,
    "league_id" bigint NOT NULL,
    "player_id" bigint NOT NULL,
    "source_name" "text" NOT NULL,
    "adjusted_rank" integer,
    "adjusted_score" numeric(10,2),
    "score_delta" numeric(10,2),
    "notes" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


--
-- Name: derived_rankings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."derived_rankings_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: derived_rankings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."derived_rankings_id_seq" OWNED BY "public"."derived_rankings"."id";


--
-- Name: draft_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."draft_rules" (
    "id" bigint NOT NULL,
    "league_id" bigint NOT NULL,
    "rules" "jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


--
-- Name: draft_rules_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."draft_rules_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: draft_rules_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."draft_rules_id_seq" OWNED BY "public"."draft_rules"."id";


--
-- Name: drop_candidates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."drop_candidates" (
    "id" bigint NOT NULL,
    "league_id" bigint NOT NULL,
    "week" integer NOT NULL,
    "player_id" bigint NOT NULL,
    "composite_score" numeric NOT NULL,
    "replacement_delta" numeric NOT NULL,
    "reason_code" "text" NOT NULL,
    "rationale" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


--
-- Name: drop_candidates_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."drop_candidates_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: drop_candidates_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."drop_candidates_id_seq" OWNED BY "public"."drop_candidates"."id";


--
-- Name: leagues; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."leagues" (
    "id" bigint NOT NULL,
    "external_league_id" bigint,
    "platform" "text" NOT NULL,
    "season" integer NOT NULL,
    "league_name" "text" NOT NULL,
    "scoring_type" "text",
    "player_rank_type" "text",
    "scoring_enhancement_type" "text",
    "team_count" integer,
    "teams_joined" integer,
    "draft_type" "text",
    "time_per_selection" integer,
    "faab_budget" integer,
    "waiver_type" "text",
    "payload" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "waiver_clear_time" timestamp with time zone,
    "add_drop_lock_window" integer,
    "in_week_adds_allowed" boolean DEFAULT true,
    "sport" "text",
    "my_team_name" "text",
    "my_team_external_id" "text",
    "external_league_key" "text" NOT NULL,
    CONSTRAINT "leagues_scoring_chk" CHECK ((("scoring_type" IS NULL) OR ("scoring_type" = ANY (ARRAY['STD'::"text", 'HALF_PPR'::"text", 'PPR'::"text", 'PICKEM'::"text"])))),
    CONSTRAINT "leagues_sport_chk" CHECK ((("sport" IS NOT NULL) AND ("sport" = ANY (ARRAY['NFL'::"text", 'NCAAF'::"text"]))))
);


--
-- Name: matchup_schedule; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."matchup_schedule" (
    "id" bigint NOT NULL,
    "sport" "text" NOT NULL,
    "season" integer NOT NULL,
    "week" integer NOT NULL,
    "team_key" "text" NOT NULL,
    "opponent_key" "text",
    "is_home" boolean,
    "is_bye" boolean DEFAULT false NOT NULL,
    "kickoff_at" timestamp with time zone,
    "game_key" "text",
    "vegas_spread" numeric(6,2),
    "vegas_total" numeric(6,2),
    "implied_points" numeric(6,2),
    "opp_def_rank_overall" numeric(6,2),
    "opp_def_ppa_pass" numeric(8,4),
    "opp_def_ppa_rush" numeric(8,4),
    "opp_pts_allowed_avg" numeric(6,2),
    "payload" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


--
-- Name: players; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."players" (
    "id" bigint NOT NULL,
    "platform" "text" NOT NULL,
    "external_player_id" bigint,
    "player_name" "text" NOT NULL,
    "first_name" "text",
    "last_name" "text",
    "pos" "text",
    "default_position_id" integer,
    "pro_team_id" integer,
    "bye_week" integer,
    "eligible_slot_names" "text",
    "percent_owned" numeric(8,2),
    "payload" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "sport" "text",
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "name_norm" "text" GENERATED ALWAYS AS ("btrim"("regexp_replace"("lower"("regexp_replace"("translate"("player_name", 'áàâäãåāéèêëēíìîïīóòôöõøōúùûüūñçýÿšžÁÀÂÄÃÅĀÉÈÊËĒÍÌÎÏĪÓÒÔÖÕØŌÚÙÛÜŪÑÇÝŸŠŽ'::"text", 'aaaaaaaeeeeeiiiiiooooooouuuuuncyyszAAAAAAAEEEEEIIIIIOOOOOOOUUUUUNCYYSZ'::"text"), '[^A-Za-z0-9 ]'::"text", ''::"text", 'g'::"text")), '\s+'::"text", ' '::"text", 'g'::"text"))) STORED,
    "name_key" "text" GENERATED ALWAYS AS ("btrim"("regexp_replace"("btrim"("regexp_replace"("lower"("regexp_replace"("translate"("player_name", 'áàâäãåāéèêëēíìîïīóòôöõøōúùûüūñçýÿšžÁÀÂÄÃÅĀÉÈÊËĒÍÌÎÏĪÓÒÔÖÕØŌÚÙÛÜŪÑÇÝŸŠŽ'::"text", 'aaaaaaaeeeeeiiiiiooooooouuuuuncyyszAAAAAAAEEEEEIIIIIOOOOOOOUUUUUNCYYSZ'::"text"), '[^A-Za-z0-9 ]'::"text", ''::"text", 'g'::"text")), '\s+'::"text", ' '::"text", 'g'::"text")), '\s+(jr|sr|ii|iii|iv|v)$'::"text", ''::"text", 'g'::"text"))) STORED,
    "external_player_key" "text",
    "injury_status" "text",
    "injury_updated_at" timestamp with time zone,
    "depth_chart_order" integer,
    CONSTRAINT "players_sport_chk" CHECK ((("sport" IS NOT NULL) AND ("sport" = ANY (ARRAY['NFL'::"text", 'NCAAF'::"text"]))))
);


--
-- Name: pro_teams; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."pro_teams" (
    "id" bigint NOT NULL,
    "platform" "text" NOT NULL,
    "external_team_id" integer NOT NULL,
    "season" integer NOT NULL,
    "team_name" "text",
    "team_abbrev" "text",
    "bye_week" integer,
    "payload" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL
);


--
-- Name: projections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."projections" (
    "id" bigint NOT NULL,
    "player_id" bigint NOT NULL,
    "source_name" "text" NOT NULL,
    "season" integer NOT NULL,
    "scoring_format" "text",
    "projected_points" numeric(10,2),
    "adp" numeric(10,2),
    "receptions" numeric(10,2),
    "pass_yd" numeric(10,2),
    "rush_yd" numeric(10,2),
    "rec_yd" numeric(10,2),
    "injury_status" "text",
    "news_summary" "text",
    "payload" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "fetched_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "week" integer DEFAULT 0 NOT NULL,
    "floor_points" numeric(10,2),
    "ceiling_points" numeric(10,2),
    "std_dev" numeric(10,2),
    "opportunity" numeric(10,2),
    "confidence" numeric(4,3),
    "ecr_rank" integer,
    "ecr_tier" integer
);


--
-- Name: roster_status_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."roster_status_history" (
    "id" bigint NOT NULL,
    "league_id" bigint,
    "player_id" bigint NOT NULL,
    "fantasy_team" "text",
    "roster_status" "text" NOT NULL,
    "position" "text",
    "fetched_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "payload" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "lineup_status" "text",
    "slot_name" "text"
);


--
-- Name: roster_status_latest; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW "public"."roster_status_latest" AS
 SELECT DISTINCT ON ("player_id", "league_id") "player_id",
    "league_id",
    "fantasy_team",
    "roster_status",
    "lineup_status",
    "slot_name",
    "position",
    "fetched_at"
   FROM "public"."roster_status_history"
  ORDER BY "player_id", "league_id",
        CASE "roster_status"
            WHEN 'owned'::"text" THEN 0
            WHEN 'waivers'::"text" THEN 1
            WHEN 'free_agent'::"text" THEN 2
            ELSE 3
        END, "fetched_at" DESC NULLS LAST;


--
-- Name: league_rosters; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW "public"."league_rosters" AS
 SELECT "l"."id" AS "league_id",
    "l"."platform",
    "l"."league_name",
    "l"."sport",
    "l"."scoring_type",
    "rsl"."fantasy_team",
    (("rsl"."fantasy_team" IS NOT NULL) AND ("rsl"."fantasy_team" = COALESCE("l"."my_team_name", ("l"."payload" ->> 'my_team_name'::"text")))) AS "is_my_team",
    "p"."id" AS "player_id",
    "p"."player_name",
    "p"."pos",
    "p"."external_player_id",
    "p"."external_player_key",
    "p"."injury_status",
    "p"."bye_week",
    "rsl"."roster_status",
    "rsl"."lineup_status",
    "rsl"."slot_name",
    "pr"."week" AS "proj_week",
    "pr"."projected_points",
    "pr"."floor_points",
    "pr"."ceiling_points",
    "pr"."std_dev",
    "pr"."opportunity",
    "pr"."ecr_rank",
    "pr"."source_name",
    "ms"."opponent_key",
    "ms"."is_bye",
    "ms"."implied_points",
    "rsl"."fetched_at"
   FROM (((("public"."roster_status_latest" "rsl"
     JOIN "public"."players" "p" ON (("p"."id" = "rsl"."player_id")))
     JOIN "public"."leagues" "l" ON (("l"."id" = "rsl"."league_id")))
     LEFT JOIN LATERAL ( SELECT "pj"."id",
            "pj"."player_id",
            "pj"."source_name",
            "pj"."season",
            "pj"."scoring_format",
            "pj"."projected_points",
            "pj"."adp",
            "pj"."receptions",
            "pj"."pass_yd",
            "pj"."rush_yd",
            "pj"."rec_yd",
            "pj"."injury_status",
            "pj"."news_summary",
            "pj"."payload",
            "pj"."fetched_at",
            "pj"."week",
            "pj"."floor_points",
            "pj"."ceiling_points",
            "pj"."std_dev",
            "pj"."opportunity",
            "pj"."confidence",
            "pj"."ecr_rank",
            "pj"."ecr_tier"
           FROM "public"."projections" "pj"
          WHERE (("pj"."player_id" = "rsl"."player_id") AND ("pj"."season" = "l"."season") AND ("pj"."source_name" =
                CASE "l"."platform"
                    WHEN 'yahoo-cfb'::"text" THEN 'cfbd_cfb_proj_yahoo'::"text"
                    WHEN 'fantrax-cfb'::"text" THEN 'cfbd_cfb_proj_fantrax'::"text"
                    WHEN 'espn-nfl'::"text" THEN 'nflverse_proj'::"text"
                    WHEN 'yahoo-nfl'::"text" THEN 'nflverse_proj'::"text"
                    WHEN 'fantrax-nfl'::"text" THEN 'nflverse_proj'::"text"
                    ELSE NULL::"text"
                END) AND (("pj"."scoring_format" = "l"."scoring_type") OR ("pj"."scoring_format" IS NULL)))
          ORDER BY ("pj"."week" = 0), "pj"."week" DESC, "pj"."fetched_at" DESC
         LIMIT 1) "pr" ON (true))
     LEFT JOIN "public"."matchup_schedule" "ms" ON ((("ms"."sport" = "l"."sport") AND ("ms"."season" = "l"."season") AND ("ms"."team_key" = ( SELECT "pt"."team_abbrev"
           FROM "public"."pro_teams" "pt"
          WHERE (("pt"."external_team_id" = "p"."pro_team_id") AND ("pt"."season" = "l"."season"))
         LIMIT 1)) AND ("ms"."week" = COALESCE("pr"."week", 0)))))
  WHERE ("rsl"."roster_status" = 'owned'::"text");


--
-- Name: league_slots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."league_slots" (
    "id" bigint NOT NULL,
    "league_id" bigint NOT NULL,
    "slot_name" "text" NOT NULL,
    "slot_count" integer NOT NULL
);


--
-- Name: league_slots_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."league_slots_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: league_slots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."league_slots_id_seq" OWNED BY "public"."league_slots"."id";


--
-- Name: leagues_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."leagues_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: leagues_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."leagues_id_seq" OWNED BY "public"."leagues"."id";


--
-- Name: matchup_schedule_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."matchup_schedule_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: matchup_schedule_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."matchup_schedule_id_seq" OWNED BY "public"."matchup_schedule"."id";


--
-- Name: my_roster; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW "public"."my_roster" AS
 SELECT "league_id",
    "platform",
    "league_name",
    "sport",
    "scoring_type",
    "fantasy_team",
    "is_my_team",
    "player_id",
    "player_name",
    "pos",
    "external_player_id",
    "external_player_key",
    "injury_status",
    "bye_week",
    "roster_status",
    "lineup_status",
    "slot_name",
    "proj_week",
    "projected_points",
    "floor_points",
    "ceiling_points",
    "std_dev",
    "opportunity",
    "ecr_rank",
    "source_name",
    "opponent_key",
    "is_bye",
    "implied_points",
    "fetched_at"
   FROM "public"."league_rosters"
  WHERE "is_my_team";


--
-- Name: roster_status_changes; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW "public"."roster_status_changes" AS
 WITH "last_two" AS (
         SELECT "roster_status_history"."league_id",
            "roster_status_history"."player_id",
            "roster_status_history"."fantasy_team",
            "roster_status_history"."roster_status",
            "roster_status_history"."fetched_at",
            "row_number"() OVER (PARTITION BY "roster_status_history"."league_id", "roster_status_history"."player_id" ORDER BY "roster_status_history"."fetched_at" DESC) AS "rn"
           FROM "public"."roster_status_history"
        ), "cur" AS (
         SELECT "last_two"."league_id",
            "last_two"."player_id",
            "last_two"."fantasy_team" AS "current_team",
            "last_two"."roster_status" AS "current_status",
            "last_two"."fetched_at" AS "latest_fetched_at"
           FROM "last_two"
          WHERE ("last_two"."rn" = 1)
        ), "prev" AS (
         SELECT "last_two"."league_id",
            "last_two"."player_id",
            "last_two"."fantasy_team" AS "previous_team",
            "last_two"."roster_status" AS "previous_status",
            "last_two"."fetched_at" AS "previous_fetched_at"
           FROM "last_two"
          WHERE ("last_two"."rn" = 2)
        )
 SELECT "p"."id" AS "player_id",
    "p"."player_name",
    "p"."pos",
    "c"."league_id",
    "pr"."previous_team",
    "c"."current_team",
    "pr"."previous_status",
    "c"."current_status",
    "pr"."previous_fetched_at",
    "c"."latest_fetched_at",
        CASE
            WHEN (("pr"."previous_team" IS NULL) AND ("c"."current_team" IS NOT NULL)) THEN 'add'::"text"
            WHEN (("pr"."previous_team" IS NOT NULL) AND ("c"."current_team" IS NULL)) THEN 'drop'::"text"
            WHEN (("pr"."previous_team" IS NOT NULL) AND ("c"."current_team" IS NOT NULL) AND ("pr"."previous_team" <> "c"."current_team")) THEN 'transfer'::"text"
            WHEN ("pr"."previous_status" IS DISTINCT FROM "c"."current_status") THEN 'status_change'::"text"
            ELSE 'no_change'::"text"
        END AS "change_type"
   FROM (("cur" "c"
     LEFT JOIN "prev" "pr" ON ((("pr"."league_id" = "c"."league_id") AND ("pr"."player_id" = "c"."player_id"))))
     JOIN "public"."players" "p" ON (("p"."id" = "c"."player_id")));


--
-- Name: opponent_moves; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW "public"."opponent_moves" AS
 SELECT "rsc"."player_id",
    "rsc"."player_name",
    "rsc"."pos",
    "rsc"."league_id",
    "rsc"."previous_team",
    "rsc"."current_team",
    "rsc"."previous_status",
    "rsc"."current_status",
    "rsc"."previous_fetched_at",
    "rsc"."latest_fetched_at",
    "rsc"."change_type",
    "l"."league_name",
    "l"."platform",
    COALESCE("l"."my_team_name", ("l"."payload" ->> 'my_team_name'::"text")) AS "my_team"
   FROM ("public"."roster_status_changes" "rsc"
     JOIN "public"."leagues" "l" ON (("l"."id" = "rsc"."league_id")))
  WHERE (("rsc"."change_type" = ANY (ARRAY['add'::"text", 'drop'::"text", 'transfer'::"text"])) AND (COALESCE("rsc"."current_team", "rsc"."previous_team") IS DISTINCT FROM COALESCE("l"."my_team_name", ("l"."payload" ->> 'my_team_name'::"text"))));


--
-- Name: cfbd_player_reference; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."cfbd_player_reference" (
    "athlete_id" "text" NOT NULL,
    "first_name" "text",
    "last_name" "text",
    "full_name" "text",
    "position" "text",
    "team" "text",
    "height" numeric(5,2),
    "weight" integer,
    "jersey" integer,
    "home_city" "text",
    "home_state" "text",
    "home_country" "text",
    "home_latitude" numeric(10,7),
    "home_longitude" numeric(10,7),
    "home_county_fips" "text",
    "recruit_ids" "text",
    "team_id" integer,
    "conference" "text",
    "division" "text",
    "classification" "text",
    "abbreviation" "text",
    "school" "text",
    "season" integer NOT NULL,
    "fetched_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "cfbd_player_reference_pkey" PRIMARY KEY ("athlete_id", "season")
);

CREATE INDEX "ix_cfbd_ref_season_athlete" ON "public"."cfbd_player_reference" USING btree ("season", "athlete_id");
CREATE INDEX "ix_cfbd_ref_team_season" ON "public"."cfbd_player_reference" USING btree ("team", "season");
CREATE INDEX "ix_cfbd_ref_position" ON "public"."cfbd_player_reference" USING btree ("position");
CREATE INDEX "ix_cfbd_ref_name" ON "public"."cfbd_player_reference" USING btree ("last_name", "first_name");


--
-- Name: cfbd_sync_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."cfbd_sync_runs" (
    "id" bigint NOT NULL,
    "season" integer NOT NULL,
    "endpoint" "text" NOT NULL,
    "fetched_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "row_count" integer NOT NULL DEFAULT 0,
    "call_count" integer NOT NULL DEFAULT 0,
    "status" "text" NOT NULL DEFAULT 'ok'::"text",
    "error_text" "text",
    "meta" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    CONSTRAINT "cfbd_sync_runs_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "cfbd_sync_runs_season_endpoint_key" UNIQUE ("season", "endpoint")
);

CREATE SEQUENCE "public"."cfbd_sync_runs_id_seq";
ALTER SEQUENCE "public"."cfbd_sync_runs_id_seq" OWNED BY "public"."cfbd_sync_runs"."id";
ALTER TABLE "public"."cfbd_sync_runs" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."cfbd_sync_runs_id_seq"'::"regclass");


--
-- Name: leagues_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."leagues_members" (
    "id" bigint NOT NULL,
    "platform" "text" NOT NULL,
    "external_member_key" "text" NOT NULL,
    "manager_name" "text",
    "manager_email" "text",
    "payload" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);

CREATE SEQUENCE "public"."leagues_members_id_seq";
ALTER SEQUENCE "public"."leagues_members_id_seq" OWNED BY "public"."leagues_members"."id";
ALTER TABLE "public"."leagues_members" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."leagues_members_id_seq"'::"regclass");

CREATE UNIQUE INDEX "ux_leagues_members_platform_extkey" ON "public"."leagues_members" USING btree ("platform", "external_member_key");


--
-- Name: league_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."league_members" (
    "id" bigint NOT NULL,
    "league_id" bigint NOT NULL,
    "member_id" bigint NOT NULL,
    "fantasy_team" "text" NOT NULL,
    "waiver_priority" integer,
    "team_slot" integer,
    "payload" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "league_members_league_id_fantasy_team_key" UNIQUE ("league_id", "fantasy_team"),
    CONSTRAINT "league_members_league_id_team_slot_key" UNIQUE ("league_id", "team_slot")
);

CREATE SEQUENCE "public"."league_members_id_seq";
ALTER SEQUENCE "public"."league_members_id_seq" OWNED BY "public"."league_members"."id";
ALTER TABLE "public"."league_members" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."league_members_id_seq"'::"regclass");

CREATE INDEX "ix_league_members_league" ON "public"."league_members" USING btree ("league_id");
CREATE INDEX "ix_league_members_member" ON "public"."league_members" USING btree ("member_id");


--
-- Name: roster_assignments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."roster_assignments" (
    "id" bigint NOT NULL,
    "league_id" bigint NOT NULL,
    "member_id" bigint NOT NULL,
    "athlete_id" "text",
    "player_id" bigint,
    "valid_from" timestamp with time zone DEFAULT "now"() NOT NULL,
    "valid_to" timestamp with time zone,
    "roster_status" "text" NOT NULL,
    "lineup_status" "text",
    "slot_name" "text",
    "source_name" "text",
    "season" integer,
    "sport" "text",
    "fetched_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "payload" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    CONSTRAINT "roster_assignments_athlete_xor_player_chk"
        CHECK ((athlete_id IS NOT NULL) OR (player_id IS NOT NULL))
);

CREATE SEQUENCE "public"."roster_assignments_id_seq";
ALTER SEQUENCE "public"."roster_assignments_id_seq" OWNED BY "public"."roster_assignments"."id";
ALTER TABLE "public"."roster_assignments" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."roster_assignments_id_seq"'::"regclass");

ALTER TABLE ONLY "public"."roster_assignments"
    ADD CONSTRAINT "roster_assignments_league_id_fkey" FOREIGN KEY ("league_id") REFERENCES "public"."leagues"("id") ON DELETE CASCADE;
ALTER TABLE ONLY "public"."roster_assignments"
    ADD CONSTRAINT "roster_assignments_member_id_fkey" FOREIGN KEY ("member_id") REFERENCES "public"."leagues_members"("id") ON DELETE CASCADE;

CREATE INDEX "ix_roster_assignments_athlete" ON "public"."roster_assignments" USING btree ("athlete_id", "sport", "season");
CREATE INDEX "ix_roster_assignments_member" ON "public"."roster_assignments" USING btree ("member_id");
CREATE INDEX "ix_roster_assignments_league" ON "public"."roster_assignments" USING btree ("league_id");
CREATE INDEX "ix_roster_assignments_active" ON "public"."roster_assignments" USING btree ("league_id", "athlete_id", "player_id") WHERE "valid_to" IS NULL;
CREATE INDEX "ix_roster_assignments_validity" ON "public"."roster_assignments" USING btree ("valid_from" DESC, "valid_to");


--
-- Name: leagues_members_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

ALTER TABLE "public"."leagues_members" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."leagues_members_id_seq"'::"regclass");

--
-- Name: league_members_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

ALTER TABLE "public"."league_members" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."league_members_id_seq"'::"regclass");

--
-- Name: roster_assignments_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

ALTER TABLE "public"."roster_assignments" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."roster_assignments_id_seq"'::"regclass");


--
-- Name: player_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."player_events" (
    "id" bigint NOT NULL,
    "player_id" bigint,
    "event_type" "text" NOT NULL,
    "severity" "text",
    "headline" "text",
    "body" "text",
    "source_name" "text" NOT NULL,
    "source_url" "text",
    "season" integer,
    "week" integer,
    "event_at" timestamp with time zone NOT NULL,
    "content_hash" "text" NOT NULL,
    "payload" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


--
-- Name: player_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."player_events_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: player_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."player_events_id_seq" OWNED BY "public"."player_events"."id";


--
-- Name: player_week_stats; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."player_week_stats" (
    "id" bigint NOT NULL,
    "player_id" bigint NOT NULL,
    "source_name" "text" NOT NULL,
    "season" integer NOT NULL,
    "week" integer NOT NULL,
    "team_key" "text",
    "opponent_key" "text",
    "snaps" integer,
    "snap_share" numeric(5,4),
    "targets" integer,
    "target_share" numeric(5,4),
    "air_yards" numeric(8,2),
    "carries" integer,
    "carry_share" numeric(5,4),
    "routes_run" integer,
    "usage_rate" numeric(5,4),
    "ppa_total" numeric(8,4),
    "pass_att" numeric(8,2),
    "pass_cmp" numeric(8,2),
    "pass_yd" numeric(8,2),
    "pass_td" numeric(6,2),
    "interceptions" numeric(6,2),
    "rush_yd" numeric(8,2),
    "rush_td" numeric(6,2),
    "rec" numeric(6,2),
    "rec_yd" numeric(8,2),
    "rec_td" numeric(6,2),
    "fumbles_lost" numeric(6,2),
    "two_pt" numeric(6,2),
    "return_td" numeric(6,2),
    "fg_made_0_39" numeric(6,2),
    "fg_made_40_49" numeric(6,2),
    "fg_made_50_plus" numeric(6,2),
    "fg_missed" numeric(6,2),
    "xp_made" numeric(6,2),
    "def_sacks" numeric(6,2),
    "def_int" numeric(6,2),
    "def_fumbles_rec" numeric(6,2),
    "def_td" numeric(6,2),
    "def_safety" numeric(6,2),
    "def_points_allowed" numeric(6,2),
    "def_yards_allowed" numeric(8,2),
    "fantasy_points_std" numeric(8,2),
    "fantasy_points_half_ppr" numeric(8,2),
    "fantasy_points_ppr" numeric(8,2),
    "played" boolean,
    "is_bye" boolean DEFAULT false NOT NULL,
    "payload" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "fetched_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


--
-- Name: player_week_stats_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."player_week_stats_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: player_week_stats_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."player_week_stats_id_seq" OWNED BY "public"."player_week_stats"."id";


--
-- Name: player_xref; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."player_xref" (
    "id" bigint NOT NULL,
    "player_id" bigint NOT NULL,
    "source_name" "text" NOT NULL,
    "source_player_key" "text" NOT NULL,
    "source_player_name" "text",
    "source_team" "text",
    "source_pos" "text",
    "confidence" double precision,
    "payload" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


--
-- Name: player_xref_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."player_xref_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: player_xref_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."player_xref_id_seq" OWNED BY "public"."player_xref"."id";


--
-- Name: players_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."players_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: players_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."players_id_seq" OWNED BY "public"."players"."id";


--
-- Name: pro_teams_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."pro_teams_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: pro_teams_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."pro_teams_id_seq" OWNED BY "public"."pro_teams"."id";


--
-- Name: projections_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."projections_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: projections_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."projections_id_seq" OWNED BY "public"."projections"."id";


--
-- Name: rankings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."rankings" (
    "id" bigint NOT NULL,
    "sport" "text" NOT NULL,
    "scoring_type" "text" NOT NULL,
    "week" integer NOT NULL,
    "player_id" bigint NOT NULL,
    "proj_pts" numeric NOT NULL,
    "opp_team" "text",
    "def_strength" numeric,
    "composite_score" numeric NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


--
-- Name: rankings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."rankings_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: rankings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."rankings_id_seq" OWNED BY "public"."rankings"."id";


--
-- Name: roster_status_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."roster_status_history_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: roster_status_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."roster_status_history_id_seq" OWNED BY "public"."roster_status_history"."id";


--
-- Name: sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."sources" (
    "id" bigint NOT NULL,
    "source_name" "text" NOT NULL,
    "source_key" "text" NOT NULL,
    "fetched_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "content_hash" "text",
    "status" "text" DEFAULT 'ok'::"text" NOT NULL,
    "meta" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL
);


--
-- Name: sources_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."sources_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: sources_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."sources_id_seq" OWNED BY "public"."sources"."id";


--
-- Name: start_sit_recommendations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."start_sit_recommendations" (
    "id" bigint NOT NULL,
    "league_id" bigint NOT NULL,
    "week" integer NOT NULL,
    "slot" "text" NOT NULL,
    "player_id" bigint NOT NULL,
    "composite_score" numeric NOT NULL,
    "recommended_action" "text" NOT NULL,
    "rationale" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


--
-- Name: start_sit_recommendations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."start_sit_recommendations_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: start_sit_recommendations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."start_sit_recommendations_id_seq" OWNED BY "public"."start_sit_recommendations"."id";


--
-- Name: sync_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."sync_runs" (
    "id" bigint NOT NULL,
    "script_name" "text" NOT NULL,
    "started_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "finished_at" timestamp with time zone,
    "status" "text" DEFAULT 'running'::"text" NOT NULL,
    "rows_written" integer DEFAULT 0 NOT NULL,
    "rows_skipped" integer DEFAULT 0 NOT NULL,
    "error_text" "text",
    "meta" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL
);


--
-- Name: sync_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."sync_runs_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: sync_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."sync_runs_id_seq" OWNED BY "public"."sync_runs"."id";


--
-- Name: waiver_targets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."waiver_targets" (
    "id" bigint NOT NULL,
    "league_id" bigint NOT NULL,
    "week" integer NOT NULL,
    "player_id" bigint NOT NULL,
    "projected_pts" numeric NOT NULL,
    "priority_score" numeric NOT NULL,
    "recommended_drop_player_id" bigint,
    "status" "text" DEFAULT 'open'::"text" NOT NULL,
    "rationale" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


--
-- Name: waiver_targets_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."waiver_targets_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: waiver_targets_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."waiver_targets_id_seq" OWNED BY "public"."waiver_targets"."id";


--
-- Name: waiver_wire; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW "public"."waiver_wire" AS
 SELECT "l"."id" AS "league_id",
    "l"."platform",
    "l"."league_name",
    "l"."sport",
    "l"."scoring_type",
    "p"."id" AS "player_id",
    "p"."player_name",
    "p"."pos",
    "p"."percent_owned",
    "p"."injury_status",
    "p"."bye_week",
    "rsl"."roster_status",
    "pr"."week" AS "proj_week",
    "pr"."projected_points",
    "pr"."floor_points",
    "pr"."ceiling_points",
    "pr"."opportunity",
    "pr"."ecr_rank",
    "pr"."source_name",
    "ms"."opponent_key",
    "ms"."is_bye",
    "ms"."implied_points",
    "rsl"."fetched_at"
   FROM (((("public"."roster_status_latest" "rsl"
     JOIN "public"."players" "p" ON (("p"."id" = "rsl"."player_id")))
     JOIN "public"."leagues" "l" ON (("l"."id" = "rsl"."league_id")))
     LEFT JOIN LATERAL ( SELECT "pj"."id",
            "pj"."player_id",
            "pj"."source_name",
            "pj"."season",
            "pj"."scoring_format",
            "pj"."projected_points",
            "pj"."adp",
            "pj"."receptions",
            "pj"."pass_yd",
            "pj"."rush_yd",
            "pj"."rec_yd",
            "pj"."injury_status",
            "pj"."news_summary",
            "pj"."payload",
            "pj"."fetched_at",
            "pj"."week",
            "pj"."floor_points",
            "pj"."ceiling_points",
            "pj"."std_dev",
            "pj"."opportunity",
            "pj"."confidence",
            "pj"."ecr_rank",
            "pj"."ecr_tier"
           FROM "public"."projections" "pj"
          WHERE (("pj"."player_id" = "rsl"."player_id") AND ("pj"."season" = "l"."season") AND ("pj"."source_name" =
                CASE "l"."platform"
                    WHEN 'yahoo-cfb'::"text" THEN 'cfbd_cfb_proj_yahoo'::"text"
                    WHEN 'fantrax-cfb'::"text" THEN 'cfbd_cfb_proj_fantrax'::"text"
                    WHEN 'espn-nfl'::"text" THEN 'nflverse_proj'::"text"
                    WHEN 'yahoo-nfl'::"text" THEN 'nflverse_proj'::"text"
                    WHEN 'fantrax-nfl'::"text" THEN 'nflverse_proj'::"text"
                    ELSE NULL::"text"
                END) AND (("pj"."scoring_format" = "l"."scoring_type") OR ("pj"."scoring_format" IS NULL)))
          ORDER BY ("pj"."week" = 0), "pj"."week" DESC, "pj"."fetched_at" DESC
         LIMIT 1) "pr" ON (true))
     LEFT JOIN "public"."matchup_schedule" "ms" ON ((("ms"."sport" = "l"."sport") AND ("ms"."season" = "l"."season") AND ("ms"."team_key" = ( SELECT "pt"."team_abbrev"
           FROM "public"."pro_teams" "pt"
          WHERE (("pt"."external_team_id" = "p"."pro_team_id") AND ("pt"."season" = "l"."season"))
         LIMIT 1)) AND ("ms"."week" = COALESCE("pr"."week", 0)))))
  WHERE ("rsl"."roster_status" = ANY (ARRAY['free_agent'::"text", 'waivers'::"text"]));


--
-- Name: derived_rankings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."derived_rankings" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."derived_rankings_id_seq"'::"regclass");


--
-- Name: draft_rules id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."draft_rules" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."draft_rules_id_seq"'::"regclass");


--
-- Name: drop_candidates id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."drop_candidates" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."drop_candidates_id_seq"'::"regclass");


--
-- Name: league_slots id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."league_slots" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."league_slots_id_seq"'::"regclass");


--
-- Name: leagues id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."leagues" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."leagues_id_seq"'::"regclass");


--
-- Name: matchup_schedule id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."matchup_schedule" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."matchup_schedule_id_seq"'::"regclass");


--
-- Name: player_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."player_events" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."player_events_id_seq"'::"regclass");


--
-- Name: player_week_stats id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."player_week_stats" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."player_week_stats_id_seq"'::"regclass");


--
-- Name: player_xref id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."player_xref" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."player_xref_id_seq"'::"regclass");


--
-- Name: players id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."players" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."players_id_seq"'::"regclass");


--
-- Name: pro_teams id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."pro_teams" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."pro_teams_id_seq"'::"regclass");


--
-- Name: projections id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."projections" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."projections_id_seq"'::"regclass");


--
-- Name: rankings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."rankings" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."rankings_id_seq"'::"regclass");


--
-- Name: roster_status_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."roster_status_history" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."roster_status_history_id_seq"'::"regclass");


--
-- Name: sources id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."sources" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."sources_id_seq"'::"regclass");


--
-- Name: start_sit_recommendations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."start_sit_recommendations" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."start_sit_recommendations_id_seq"'::"regclass");


--
-- Name: sync_runs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."sync_runs" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."sync_runs_id_seq"'::"regclass");


--
-- Name: waiver_targets id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."waiver_targets" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."waiver_targets_id_seq"'::"regclass");


--
-- Name: cfbd_player_overrides cfbd_player_overrides_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."cfbd_player_overrides"
    ADD CONSTRAINT "cfbd_player_overrides_pkey" PRIMARY KEY ("platform", "player_name", "pos");


--
-- Name: derived_rankings derived_rankings_league_id_player_id_source_name_created_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."derived_rankings"
    ADD CONSTRAINT "derived_rankings_league_id_player_id_source_name_created_at_key" UNIQUE ("league_id", "player_id", "source_name", "created_at");


--
-- Name: derived_rankings derived_rankings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."derived_rankings"
    ADD CONSTRAINT "derived_rankings_pkey" PRIMARY KEY ("id");


--
-- Name: draft_rules draft_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."draft_rules"
    ADD CONSTRAINT "draft_rules_pkey" PRIMARY KEY ("id");


--
-- Name: drop_candidates drop_candidates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."drop_candidates"
    ADD CONSTRAINT "drop_candidates_pkey" PRIMARY KEY ("id");


--
-- Name: league_slots league_slots_league_id_slot_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."league_slots"
    ADD CONSTRAINT "league_slots_league_id_slot_name_key" UNIQUE ("league_id", "slot_name");


--
-- Name: league_slots league_slots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."league_slots"
    ADD CONSTRAINT "league_slots_pkey" PRIMARY KEY ("id");


--
-- Name: leagues leagues_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."leagues"
    ADD CONSTRAINT "leagues_pkey" PRIMARY KEY ("id");


--
-- Name: leagues leagues_platform_extkey_season_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."leagues"
    ADD CONSTRAINT "leagues_platform_extkey_season_key" UNIQUE ("platform", "external_league_key", "season");


--
-- Name: matchup_schedule matchup_schedule_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."matchup_schedule"
    ADD CONSTRAINT "matchup_schedule_pkey" PRIMARY KEY ("id");


--
-- Name: matchup_schedule matchup_schedule_sport_season_week_team_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."matchup_schedule"
    ADD CONSTRAINT "matchup_schedule_sport_season_week_team_key_key" UNIQUE ("sport", "season", "week", "team_key");


--
-- Name: player_events player_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."player_events"
    ADD CONSTRAINT "player_events_pkey" PRIMARY KEY ("id");


--
-- Name: player_events player_events_player_id_source_name_content_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."player_events"
    ADD CONSTRAINT "player_events_player_id_source_name_content_hash_key" UNIQUE NULLS NOT DISTINCT ("player_id", "source_name", "content_hash");


--
-- Name: player_events player_events_type_chk; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE "public"."player_events"
    ADD CONSTRAINT "player_events_type_chk" CHECK (("event_type" = ANY (ARRAY['injury'::"text", 'news'::"text", 'depth_chart'::"text", 'practice'::"text", 'transaction'::"text"])));


--
-- Name: player_week_stats player_week_stats_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."player_week_stats"
    ADD CONSTRAINT "player_week_stats_pkey" PRIMARY KEY ("id");


--
-- Name: player_week_stats player_week_stats_player_id_source_name_season_week_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."player_week_stats"
    ADD CONSTRAINT "player_week_stats_player_id_source_name_season_week_key" UNIQUE ("player_id", "source_name", "season", "week");


--
-- Name: player_xref player_xref_confidence_chk; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE "public"."player_xref"
    ADD CONSTRAINT "player_xref_confidence_chk" CHECK ((("confidence" IS NULL) OR (("confidence" >= (0.0)::double precision) AND ("confidence" <= (1.0)::double precision))));


--
-- Name: player_xref player_xref_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."player_xref"
    ADD CONSTRAINT "player_xref_pkey" PRIMARY KEY ("id");


--
-- Name: player_xref player_xref_source_name_source_player_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."player_xref"
    ADD CONSTRAINT "player_xref_source_name_source_player_key_key" UNIQUE ("source_name", "source_player_key");


--
-- Name: players players_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."players"
    ADD CONSTRAINT "players_pkey" PRIMARY KEY ("id");


--
-- Name: players players_platform_external_player_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."players"
    ADD CONSTRAINT "players_platform_external_player_id_key" UNIQUE ("platform", "external_player_id");


--
-- Name: pro_teams pro_teams_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."pro_teams"
    ADD CONSTRAINT "pro_teams_pkey" PRIMARY KEY ("id");


--
-- Name: pro_teams pro_teams_platform_external_team_id_season_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."pro_teams"
    ADD CONSTRAINT "pro_teams_platform_external_team_id_season_key" UNIQUE ("platform", "external_team_id", "season");


--
-- Name: projections projections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."projections"
    ADD CONSTRAINT "projections_pkey" PRIMARY KEY ("id");


--
-- Name: rankings rankings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."rankings"
    ADD CONSTRAINT "rankings_pkey" PRIMARY KEY ("id");


--
-- Name: roster_status_history roster_status_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."roster_status_history"
    ADD CONSTRAINT "roster_status_history_pkey" PRIMARY KEY ("id");


--
-- Name: roster_status_history rsh_lineup_status_chk; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE "public"."roster_status_history"
    ADD CONSTRAINT "rsh_lineup_status_chk" CHECK ((("lineup_status" IS NULL) OR ("lineup_status" = ANY (ARRAY['starter'::"text", 'bench'::"text", 'ir'::"text", 'taxi'::"text"]))));


--
-- Name: roster_status_history rsh_roster_status_chk; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE "public"."roster_status_history"
    ADD CONSTRAINT "rsh_roster_status_chk" CHECK (("roster_status" = ANY (ARRAY['owned'::"text", 'waivers'::"text", 'free_agent'::"text"])));


--
-- Name: sources sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."sources"
    ADD CONSTRAINT "sources_pkey" PRIMARY KEY ("id");


--
-- Name: start_sit_recommendations start_sit_recommendations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."start_sit_recommendations"
    ADD CONSTRAINT "start_sit_recommendations_pkey" PRIMARY KEY ("id");


--
-- Name: sync_runs sync_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."sync_runs"
    ADD CONSTRAINT "sync_runs_pkey" PRIMARY KEY ("id");


--
-- Name: waiver_targets waiver_targets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."waiver_targets"
    ADD CONSTRAINT "waiver_targets_pkey" PRIMARY KEY ("id");


--
-- Name: drop_candidates_league_week_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "drop_candidates_league_week_idx" ON "public"."drop_candidates" USING "btree" ("league_id", "week");


--
-- Name: ix_player_events_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_player_events_recent" ON "public"."player_events" USING "btree" ("player_id", "event_at" DESC);


--
-- Name: ix_player_events_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_player_events_type" ON "public"."player_events" USING "btree" ("event_type", "event_at" DESC);


--
-- Name: ix_player_xref_player; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_player_xref_player" ON "public"."player_xref" USING "btree" ("source_name", "player_id");


--
-- Name: ix_players_injury; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_players_injury" ON "public"."players" USING "btree" ("injury_status") WHERE ("injury_status" IS NOT NULL);


--
-- Name: ix_players_name_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_players_name_key" ON "public"."players" USING "btree" ("name_key", "pos");


--
-- Name: ix_players_name_norm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_players_name_norm" ON "public"."players" USING "btree" ("name_norm", "pos");


--
-- Name: ix_players_platform_sport; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_players_platform_sport" ON "public"."players" USING "btree" ("platform", "sport");


--
-- Name: ix_players_sport; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_players_sport" ON "public"."players" USING "btree" ("sport");


--
-- Name: ix_pws_player_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_pws_player_recent" ON "public"."player_week_stats" USING "btree" ("player_id", "season", "week" DESC);


--
-- Name: ix_pws_season_week; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_pws_season_week" ON "public"."player_week_stats" USING "btree" ("season", "week");


--
-- Name: ix_roster_history_league_fetched; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_roster_history_league_fetched" ON "public"."roster_status_history" USING "btree" ("league_id", "fetched_at" DESC);


--
-- Name: ix_roster_history_player_fetched; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_roster_history_player_fetched" ON "public"."roster_status_history" USING "btree" ("player_id", "fetched_at" DESC);


--
-- Name: ix_rsh_latest_sort; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_rsh_latest_sort" ON "public"."roster_status_history" USING "btree" ("player_id", "league_id", (
CASE "roster_status"
    WHEN 'owned'::"text" THEN 0
    WHEN 'waivers'::"text" THEN 1
    WHEN 'free_agent'::"text" THEN 2
    ELSE 3
END), "fetched_at" DESC);


--
-- Name: ix_sync_runs_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_sync_runs_recent" ON "public"."sync_runs" USING "btree" ("script_name", "started_at" DESC);


--
-- Name: rankings_week_sport_scoring_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "rankings_week_sport_scoring_idx" ON "public"."rankings" USING "btree" ("sport", "scoring_type", "week");


--
-- Name: ssr_league_week_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ssr_league_week_idx" ON "public"."start_sit_recommendations" USING "btree" ("league_id", "week");


--
-- Name: uix_players_platform_name_pos; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX "uix_players_platform_name_pos" ON "public"."players" USING "btree" ("platform", "player_name", "pos") WHERE ("external_player_id" IS NULL);


--
-- Name: ux_players_platform_extkey; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX "ux_players_platform_extkey" ON "public"."players" USING "btree" ("platform", "external_player_key") WHERE ("external_player_key" IS NOT NULL);


--
-- Name: ux_projections_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX "ux_projections_key" ON "public"."projections" USING "btree" ("player_id", "source_name", "season", "week", COALESCE("scoring_format", ''::"text"));


--
-- Name: ux_sources_name_key_fetched; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX "ux_sources_name_key_fetched" ON "public"."sources" USING "btree" ("source_name", "source_key", "fetched_at");


--
-- Name: waiver_targets_league_week_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "waiver_targets_league_week_idx" ON "public"."waiver_targets" USING "btree" ("league_id", "week");


--
-- Name: matchup_schedule trg_matchup_set_implied; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER "trg_matchup_set_implied" BEFORE INSERT OR UPDATE ON "public"."matchup_schedule" FOR EACH ROW EXECUTE FUNCTION "public"."matchup_set_implied"();


--
-- Name: derived_rankings derived_rankings_league_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."derived_rankings"
    ADD CONSTRAINT "derived_rankings_league_id_fkey" FOREIGN KEY ("league_id") REFERENCES "public"."leagues"("id") ON DELETE CASCADE;


--
-- Name: derived_rankings derived_rankings_player_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."derived_rankings"
    ADD CONSTRAINT "derived_rankings_player_id_fkey" FOREIGN KEY ("player_id") REFERENCES "public"."players"("id") ON DELETE CASCADE;


--
-- Name: draft_rules draft_rules_league_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."draft_rules"
    ADD CONSTRAINT "draft_rules_league_id_fkey" FOREIGN KEY ("league_id") REFERENCES "public"."leagues"("id") ON DELETE CASCADE;


--
-- Name: drop_candidates drop_candidates_league_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."drop_candidates"
    ADD CONSTRAINT "drop_candidates_league_id_fkey" FOREIGN KEY ("league_id") REFERENCES "public"."leagues"("id") ON DELETE CASCADE;


--
-- Name: drop_candidates drop_candidates_player_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."drop_candidates"
    ADD CONSTRAINT "drop_candidates_player_id_fkey" FOREIGN KEY ("player_id") REFERENCES "public"."players"("id") ON DELETE CASCADE;


--
-- Name: league_slots league_slots_league_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."league_slots"
    ADD CONSTRAINT "league_slots_league_id_fkey" FOREIGN KEY ("league_id") REFERENCES "public"."leagues"("id") ON DELETE CASCADE;


--
-- Name: player_events player_events_player_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."player_events"
    ADD CONSTRAINT "player_events_player_id_fkey" FOREIGN KEY ("player_id") REFERENCES "public"."players"("id") ON DELETE CASCADE;


--
-- Name: player_week_stats player_week_stats_player_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."player_week_stats"
    ADD CONSTRAINT "player_week_stats_player_id_fkey" FOREIGN KEY ("player_id") REFERENCES "public"."players"("id") ON DELETE CASCADE;


--
-- Name: player_xref player_xref_player_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."player_xref"
    ADD CONSTRAINT "player_xref_player_id_fkey" FOREIGN KEY ("player_id") REFERENCES "public"."players"("id") ON DELETE CASCADE;


--
-- Name: projections projections_player_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."projections"
    ADD CONSTRAINT "projections_player_id_fkey" FOREIGN KEY ("player_id") REFERENCES "public"."players"("id") ON DELETE CASCADE;


--
-- Name: rankings rankings_player_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."rankings"
    ADD CONSTRAINT "rankings_player_id_fkey" FOREIGN KEY ("player_id") REFERENCES "public"."players"("id") ON DELETE CASCADE;


--
-- Name: roster_status_history roster_status_history_league_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."roster_status_history"
    ADD CONSTRAINT "roster_status_history_league_id_fkey" FOREIGN KEY ("league_id") REFERENCES "public"."leagues"("id") ON DELETE CASCADE;


--
-- Name: roster_status_history roster_status_history_player_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."roster_status_history"
    ADD CONSTRAINT "roster_status_history_player_id_fkey" FOREIGN KEY ("player_id") REFERENCES "public"."players"("id") ON DELETE CASCADE;


--
-- Name: start_sit_recommendations start_sit_recommendations_league_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."start_sit_recommendations"
    ADD CONSTRAINT "start_sit_recommendations_league_id_fkey" FOREIGN KEY ("league_id") REFERENCES "public"."leagues"("id") ON DELETE CASCADE;


--
-- Name: start_sit_recommendations start_sit_recommendations_player_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."start_sit_recommendations"
    ADD CONSTRAINT "start_sit_recommendations_player_id_fkey" FOREIGN KEY ("player_id") REFERENCES "public"."players"("id") ON DELETE CASCADE;


--
-- Name: waiver_targets waiver_targets_league_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."waiver_targets"
    ADD CONSTRAINT "waiver_targets_league_id_fkey" FOREIGN KEY ("league_id") REFERENCES "public"."leagues"("id") ON DELETE CASCADE;


--
-- Name: waiver_targets waiver_targets_player_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."waiver_targets"
    ADD CONSTRAINT "waiver_targets_player_id_fkey" FOREIGN KEY ("player_id") REFERENCES "public"."players"("id") ON DELETE CASCADE;


--
-- Name: waiver_targets waiver_targets_recommended_drop_player_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."waiver_targets"
    ADD CONSTRAINT "waiver_targets_recommended_drop_player_id_fkey" FOREIGN KEY ("recommended_drop_player_id") REFERENCES "public"."players"("id");


--
-- PostgreSQL database dump complete
--

--

