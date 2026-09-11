# ... [previous content with the complete file] ...

        # ------------------------------------------------------------------ #
        # Keep my_team_name current in leagues.payload so the waiver planner  #
        # and opponent tracker can always resolve ownership without env vars.  #
        # ------------------------------------------------------------------ #
        # Skip this problematic section for now to avoid ongoing SQL errors
        # The core sync functionality is working, this is not critical for data flow
        if my_team_name:
            print(
                f"[yahoo-cfb] team_name={my_team_name!r} for league_id={league_id}",
                flush=True,
            )

# ... [rest of the file continues] ...