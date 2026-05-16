"""SQLAlchemy ORM models for the meal planner database."""

from datetime import UTC, date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Ingredient(Base):
    __tablename__ = "ingredients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    source_label: Mapped[str] = mapped_column(String(100), nullable=False)
    location: Mapped[str] = mapped_column(String(20), nullable=False, default="fresh")
    subcategory: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    arrived_date: Mapped[date] = mapped_column(Date, nullable=False)
    best_before: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    usda_fdc_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    open_food_facts_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    calories_per_100g: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    protein_per_100g: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fibre_per_100g: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    meal_uses: Mapped[list["MealIngredient"]] = relationship(
        "MealIngredient", back_populates="ingredient"
    )


class Meal(Base):
    __tablename__ = "meals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    cuisine_tag: Mapped[str] = mapped_column(String(100), nullable=False)
    cooked_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_portions: Mapped[int] = mapped_column(Integer, nullable=False)
    portions_remaining: Mapped[int] = mapped_column(Integer, nullable=False)
    location: Mapped[str] = mapped_column(String(20), nullable=False, default="freezer")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    ingredients: Mapped[list["MealIngredient"]] = relationship(
        "MealIngredient", back_populates="meal"
    )
    nutrition_logs: Mapped[list["NutritionLog"]] = relationship(
        "NutritionLog", back_populates="source_meal"
    )


class MealIngredient(Base):
    __tablename__ = "meal_ingredients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meal_id: Mapped[int] = mapped_column(ForeignKey("meals.id"), nullable=False)
    # Nullable so history survives when an ingredient is later deleted from inventory
    ingredient_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("ingredients.id", ondelete="SET NULL"), nullable=True
    )
    quantity_used: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)

    meal: Mapped["Meal"] = relationship("Meal", back_populates="ingredients")
    ingredient: Mapped[Optional["Ingredient"]] = relationship("Ingredient", back_populates="meal_uses")


class NutritionLog(Base):
    __tablename__ = "nutrition_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    source_meal_id: Mapped[Optional[int]] = mapped_column(ForeignKey("meals.id"), nullable=True)
    calories: Mapped[float] = mapped_column(Float, nullable=False)
    protein_g: Mapped[float] = mapped_column(Float, nullable=False)
    fibre_g: Mapped[float] = mapped_column(Float, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    source_meal: Mapped[Optional["Meal"]] = relationship("Meal", back_populates="nutrition_logs")


class Preference(Base):
    __tablename__ = "preferences"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class MealPlan(Base):
    __tablename__ = "meal_plan"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    cuisine_tag: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    planned_date: Mapped[date] = mapped_column(Date, nullable=False)
    servings: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="planned")
    source_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    ingredients: Mapped[list["MealPlanIngredient"]] = relationship(
        "MealPlanIngredient", back_populates="plan", cascade="all, delete-orphan"
    )


class MealPlanIngredient(Base):
    __tablename__ = "meal_plan_ingredients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("meal_plan.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    plan: Mapped["MealPlan"] = relationship("MealPlan", back_populates="ingredients")


class DeliverySchedule(Base):
    __tablename__ = "delivery_schedule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_label: Mapped[str] = mapped_column(String(100), nullable=False)
    expected_date: Mapped[date] = mapped_column(Date, nullable=False)
    scraped_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    raw_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
