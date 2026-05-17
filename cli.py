"""Typer CLI for meal planner admin tasks."""

import asyncio
import os
from datetime import date, timedelta

import typer

app = typer.Typer(help="Meal Planner admin CLI")


@app.command(name="register-webhook")
def register_webhook(
    url: str = typer.Argument(help="Full webhook URL, e.g. https://puffin-meal-planner.fly.dev/webhook/telegram"),
) -> None:
    """Register (or re-register) the Telegram webhook, including the secret token if set."""
    import httpx

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        typer.echo("Error: TELEGRAM_BOT_TOKEN not set", err=True)
        raise typer.Exit(1)

    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    payload: dict = {"url": url}
    if secret:
        payload["secret_token"] = secret

    async def _register() -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{bot_token}/setWebhook",
                json=payload,
            )
            return r.json()

    result = asyncio.run(_register())
    if result.get("ok"):
        typer.echo("Webhook registered.")
        if secret:
            typer.echo("Secret token set — requests without the correct header will be rejected.")
    else:
        typer.echo(f"Failed: {result}", err=True)
        raise typer.Exit(1)


@app.command()
def seed(
    force: bool = typer.Option(False, "--force", help="Re-seed even if data already exists"),
) -> None:
    """Seed preferences, sample ingredients, batch-cooked meals, and nutrition logs."""
    asyncio.run(_seed(force=force))


@app.command(name="list-sources")
def list_sources() -> None:
    """List all registered food sources."""
    import sources.registry as registry  # triggers auto-registration

    sources = registry.list_all()
    if not sources:
        typer.echo("No sources registered.")
        return
    typer.echo(f"{'Label':<25}  Description")
    typer.echo("-" * 70)
    for label, source in sorted(sources.items()):
        typer.echo(f"{label:<25}  {source.describe()}")


async def _seed(force: bool = False) -> None:
    from db.database import create_all_tables, AsyncSessionLocal
    from db import crud

    await create_all_tables()

    async with AsyncSessionLocal() as session:
        existing = await crud.get_all_preferences(session)
        if existing and not force:
            typer.echo(
                "Database already has preference data. "
                "Use --force to re-seed."
            )
            return

        typer.echo("Seeding preferences...")
        preferences = {
            "cultural_home": "british",
            "cuisine_openness": "high",
            "weekday_max_cook_minutes": "30",
            "weekend_max_cook_minutes": "120",
            "calorie_target": "2200",
            "protein_target_g": "140",
            "fibre_target_g": "30",
            "dislikes": "offal,blue_cheese",
            "batch_cook_portions_target": "4",
            "freezer_first": "true",
        }
        for key, value in preferences.items():
            await crud.set_preference(session, key, value)
        typer.echo(f"  {len(preferences)} preferences seeded.")

        typer.echo("Seeding ingredients...")
        today = date.today()
        ingredients_data = [
            dict(
                name="courgettes",
                quantity=3.0,
                unit="whole",
                source_label="manual",
                location="fresh",
                subcategory="veg",
                arrived_date=today - timedelta(days=2),
            ),
            dict(
                name="aubergine",
                quantity=1.0,
                unit="whole",
                source_label="manual",
                location="fresh",
                subcategory="veg",
                arrived_date=today - timedelta(days=2),
            ),
            dict(
                name="chicken thighs",
                quantity=800.0,
                unit="g",
                source_label="manual",
                location="fresh",
                subcategory="meat",
                arrived_date=today - timedelta(days=1),
                best_before=today + timedelta(days=1),  # expiry risk
            ),
            dict(
                name="red lentils",
                quantity=500.0,
                unit="g",
                source_label="manual",
                location="pantry",
                subcategory="legume",
                arrived_date=today - timedelta(days=6),
            ),
            dict(
                name="tinned tomatoes",
                quantity=4.0,
                unit="tin",
                source_label="manual",
                location="pantry",
                subcategory="condiment",
                arrived_date=today - timedelta(days=15),
            ),
            dict(
                name="mature cheddar",
                quantity=200.0,
                unit="g",
                source_label="manual",
                location="fresh",
                subcategory="dairy",
                arrived_date=today - timedelta(days=3),
                best_before=today + timedelta(days=14),
            ),
            dict(
                name="brown onions",
                quantity=6.0,
                unit="whole",
                source_label="manual",
                location="pantry",
                subcategory="veg",
                arrived_date=today - timedelta(days=6),
            ),
            dict(
                name="free-range eggs",
                quantity=12.0,
                unit="whole",
                source_label="manual",
                location="fresh",
                subcategory="eggs",
                arrived_date=today - timedelta(days=2),
                best_before=today + timedelta(days=26),
            ),
            dict(
                name="spinach",
                quantity=150.0,
                unit="g",
                source_label="manual",
                location="fresh",
                subcategory="veg",
                arrived_date=today - timedelta(days=3),
                best_before=today + timedelta(days=1),  # expiry risk
            ),
            dict(
                name="basmati rice",
                quantity=1.0,
                unit="kg",
                source_label="manual",
                location="pantry",
                subcategory="grain",
                arrived_date=today - timedelta(days=15),
            ),
        ]
        ingredients = []
        for data in ingredients_data:
            ing = await crud.create_ingredient(session, **data)
            ingredients.append(ing)
        typer.echo(f"  {len(ingredients)} ingredients seeded.")

        typer.echo("Seeding batch-cooked meals...")
        meal1 = await crud.create_meal(
            session,
            name="Chicken and Lentil Dal",
            cuisine_tag="south-asian",
            cooked_date=today - timedelta(days=3),
            total_portions=4,
            portions_remaining=4,
            location="freezer",
            notes="Batch cook. Good with rice or naan.",
        )
        meal2 = await crud.create_meal(
            session,
            name="Lamb and Tomato Ragu",
            cuisine_tag="italian",
            cooked_date=today - timedelta(days=5),
            total_portions=4,
            portions_remaining=3,
            location="freezer",
            notes="Batch cook. One portion eaten from fridge.",
        )
        typer.echo(f"  2 meals seeded (IDs {meal1.id}, {meal2.id}).")

        typer.echo("Seeding nutrition logs...")
        logs_data = [
            dict(log_date=today - timedelta(days=2), calories=2100.0, protein_g=135.0, fibre_g=28.0),
            dict(log_date=today - timedelta(days=1), calories=2300.0, protein_g=150.0, fibre_g=32.0),
            dict(
                log_date=today,
                calories=850.0,
                protein_g=55.0,
                fibre_g=14.0,
                notes="Partial — morning only",
            ),
        ]
        for data in logs_data:
            await crud.create_nutrition_log(session, **data)
        typer.echo(f"  {len(logs_data)} nutrition log entries seeded.")

    typer.echo("Seed complete.")


if __name__ == "__main__":
    app()
