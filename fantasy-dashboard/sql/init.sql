--
-- PostgreSQL database dump
--

\restrict gg13hgiOlCiVim0AohZSeeMB2Px55Cuw1fohIgSHSb9QOYRDaGWoTDSh8kxJP9w

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
    "external_league_id" bigint NOT NULL,
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
    "my_team_external_id" "text"
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
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
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
    "fetched_at" timestamp with time zone DEFAULT "now"() NOT NULL
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
    "payload" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL
);


--
-- Name: roster_status_latest; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW "public"."roster_status_latest" AS
 SELECT DISTINCT ON ("player_id", "league_id") "player_id",
    "league_id",
    "fantasy_team",
    "roster_status",
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
    "rsl"."fantasy_team",
    ("rsl"."fantasy_team" = ("l"."payload" ->> 'my_team_name'::"text")) AS "is_my_team",
    "p"."player_name",
    "p"."pos",
    "p"."external_player_id",
    "rsl"."roster_status",
    "pr"."projected_points",
    "pr"."source_name",
    "rsl"."fetched_at"
   FROM ((("public"."roster_status_latest" "rsl"
     JOIN "public"."players" "p" ON (("p"."id" = "rsl"."player_id")))
     JOIN "public"."leagues" "l" ON (("l"."id" = "rsl"."league_id")))
     LEFT JOIN "public"."projections" "pr" ON ((("pr"."player_id" = "rsl"."player_id") AND ("pr"."source_name" =
        CASE "l"."platform"
            WHEN 'yahoo-cfb'::"text" THEN 'cfbd_cfb_proj_yahoo'::"text"
            WHEN 'fantrax-cfb'::"text" THEN 'cfbd_cfb_proj_fantrax'::"text"
            WHEN 'espn-nfl'::"text" THEN 'fantasypros_proj'::"text"
            WHEN 'yahoo-nfl'::"text" THEN 'fantasypros_proj'::"text"
            ELSE NULL::"text"
        END))))
  WHERE ("rsl"."roster_status" = 'owned'::"text")
  ORDER BY "l"."id", "rsl"."fantasy_team", "p"."pos", "pr"."projected_points" DESC NULLS LAST;


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
-- Name: pro_teams; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."pro_teams" (
    "id" bigint NOT NULL,
    "platform" "text" DEFAULT 'espn'::"text" NOT NULL,
    "external_team_id" integer NOT NULL,
    "season" integer NOT NULL,
    "team_name" "text",
    "team_abbrev" "text",
    "bye_week" integer,
    "payload" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL
);


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
        ), "current_rows" AS (
         SELECT "last_two"."league_id",
            "last_two"."player_id",
            "last_two"."fantasy_team" AS "current_team",
            "last_two"."roster_status" AS "current_status",
            "last_two"."fetched_at" AS "latest_fetched_at"
           FROM "last_two"
          WHERE ("last_two"."rn" = 1)
        ), "previous_rows" AS (
         SELECT "last_two"."league_id",
            "last_two"."player_id",
            "last_two"."fantasy_team" AS "previous_team",
            "last_two"."roster_status" AS "previous_status",
            "last_two"."fetched_at" AS "previous_fetched_at"
           FROM "last_two"
          WHERE ("last_two"."rn" = 2)
        )
 SELECT "p"."id" AS "player_id",
    "c"."league_id",
    "pr"."previous_team",
    "c"."current_team",
    "pr"."previous_status",
    "c"."current_status",
    "c"."latest_fetched_at",
        CASE
            WHEN (("pr"."previous_team" IS NULL) AND ("c"."current_team" IS NOT NULL)) THEN 'add'::"text"
            WHEN (("pr"."previous_team" IS NOT NULL) AND ("c"."current_team" IS NULL)) THEN 'drop'::"text"
            WHEN ("pr"."previous_status" IS DISTINCT FROM "c"."current_status") THEN 'status_change'::"text"
            ELSE 'no_change'::"text"
        END AS "change_type"
   FROM (("current_rows" "c"
     LEFT JOIN "previous_rows" "pr" ON ((("pr"."league_id" = "c"."league_id") AND ("pr"."player_id" = "c"."player_id"))))
     JOIN "public"."players" "p" ON (("p"."id" = "c"."player_id")));


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
    "p"."player_name",
    "p"."pos",
    "rsl"."roster_status",
    "p"."percent_owned",
    "pr"."projected_points",
    "pr"."source_name",
    "rsl"."fetched_at"
   FROM ((("public"."roster_status_latest" "rsl"
     JOIN "public"."players" "p" ON (("p"."id" = "rsl"."player_id")))
     JOIN "public"."leagues" "l" ON (("l"."id" = "rsl"."league_id")))
     LEFT JOIN "public"."projections" "pr" ON ((("pr"."player_id" = "rsl"."player_id") AND ("pr"."source_name" =
        CASE "l"."platform"
            WHEN 'yahoo-cfb'::"text" THEN 'cfbd_cfb_proj_yahoo'::"text"
            WHEN 'fantrax-cfb'::"text" THEN 'cfbd_cfb_proj_fantrax'::"text"
            WHEN 'espn-nfl'::"text" THEN 'fantasypros_proj'::"text"
            WHEN 'yahoo-nfl'::"text" THEN 'fantasypros_proj'::"text"
            ELSE NULL::"text"
        END))))
  WHERE ("rsl"."roster_status" = ANY (ARRAY['free_agent'::"text", 'waivers'::"text"]))
  ORDER BY "l"."id", "pr"."projected_points" DESC NULLS LAST;


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
-- Name: leagues leagues_external_league_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."leagues"
    ADD CONSTRAINT "leagues_external_league_id_key" UNIQUE ("external_league_id");


--
-- Name: leagues leagues_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."leagues"
    ADD CONSTRAINT "leagues_pkey" PRIMARY KEY ("id");


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
-- Name: waiver_targets waiver_targets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."waiver_targets"
    ADD CONSTRAINT "waiver_targets_pkey" PRIMARY KEY ("id");


--
-- Name: drop_candidates_league_week_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "drop_candidates_league_week_idx" ON "public"."drop_candidates" USING "btree" ("league_id", "week");


--
-- Name: ix_players_sport; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_players_sport" ON "public"."players" USING "btree" ("sport");


--
-- Name: ix_roster_history_league_fetched; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_roster_history_league_fetched" ON "public"."roster_status_history" USING "btree" ("league_id", "fetched_at" DESC);


--
-- Name: ix_roster_history_player_fetched; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_roster_history_player_fetched" ON "public"."roster_status_history" USING "btree" ("player_id", "fetched_at" DESC);


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
-- Name: ux_sources_name_key_fetched; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX "ux_sources_name_key_fetched" ON "public"."sources" USING "btree" ("source_name", "source_key", "fetched_at");


--
-- Name: waiver_targets_league_week_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "waiver_targets_league_week_idx" ON "public"."waiver_targets" USING "btree" ("league_id", "week");


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

\unrestrict gg13hgiOlCiVim0AohZSeeMB2Px55Cuw1fohIgSHSb9QOYRDaGWoTDSh8kxJP9w

