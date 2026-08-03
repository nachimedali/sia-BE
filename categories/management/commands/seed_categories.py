"""Seeds the vertical tree (design.md §6.1).

The six roots are the ones the onboarding template offers; the children exist so
trend extraction can be scoped tightly (a "Skincare" corpus is a much better
grounding signal than a "Beauty & Wellness" one).

Idempotent — safe to re-run on every deploy.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.text import slugify

from categories.models import Category

# Root -> children. Roots match final/onboarding-3.html so the wizard's chips
# resolve to real rows.
TREE: dict[str, list[str]] = {
    "Home & Lifestyle": [
        "Furniture",
        "Homeware & Ceramics",
        "Decor & Art",
        "Garden & Outdoor",
        "Cleaning & Organisation",
        "Candles & Fragrance",
    ],
    "Fashion & Apparel": [
        "Womenswear",
        "Menswear",
        "Footwear",
        "Accessories",
        "Jewellery",
        "Kidswear",
        "Vintage & Resale",
    ],
    "Food & Beverage": [
        "Coffee & Tea",
        "Bakery & Desserts",
        "Snacks & Pantry",
        "Restaurants & Cafés",
        "Meal Kits",
        "Alcohol & Spirits",
        "Supplements & Nutrition",
    ],
    "Beauty & Wellness": [
        "Skincare",
        "Haircare",
        "Cosmetics",
        "Fragrance",
        "Spa & Salon",
        "Mental Health & Mindfulness",
    ],
    "Sports & Outdoors": [
        "Fitness & Gym",
        "Running",
        "Cycling",
        "Hiking & Camping",
        "Water Sports",
        "Team Sports",
        "Yoga & Pilates",
    ],
    "Education": [
        "Online Courses",
        "Tutoring",
        "Language Learning",
        "Kids & Early Years",
        "Professional Certification",
        "Coaching",
    ],
    # Beyond the wizard's six, so every vertical has a home (D11).
    "Services & Professional": [
        "Agencies & Consulting",
        "Real Estate",
        "Legal & Finance",
        "Trades & Home Services",
        "Events & Weddings",
        "Photography & Video",
    ],
    "Technology & SaaS": [
        "Developer Tools",
        "Productivity Software",
        "Consumer Apps",
        "Hardware & Gadgets",
        "AI & Data",
    ],
    "Health & Medical": [
        "Clinics & Practices",
        "Dental",
        "Veterinary",
        "Medical Devices",
        "Pharmacy",
    ],
    "Travel & Hospitality": [
        "Hotels & Stays",
        "Tours & Experiences",
        "Airlines & Transport",
        "Travel Gear",
    ],
    "Pets": ["Pet Food", "Pet Accessories", "Grooming & Care"],
    "Automotive": ["Dealerships", "Parts & Accessories", "Detailing & Repair", "EV & Mobility"],
    "Arts & Entertainment": [
        "Music & Artists",
        "Film & Streaming",
        "Gaming & Esports",
        "Books & Publishing",
        "Live Events",
    ],
    "Nonprofit & Community": ["Charities", "Religious Organisations", "Advocacy & Causes"],
}


class Command(BaseCommand):
    help = "Seed the category tree covering all verticals."

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        roots = 0
        children = 0

        for root_name, child_names in TREE.items():
            root, created = Category.objects.update_or_create(
                slug=slugify(root_name),
                defaults={"name": root_name, "parent": None, "is_active": True},
            )
            roots += 1 if created else 0

            for child_name in child_names:
                _, child_created = Category.objects.update_or_create(
                    slug=slugify(f"{root_name} {child_name}"),
                    defaults={"name": child_name, "parent": root, "is_active": True},
                )
                children += 1 if child_created else 0

        total = Category.objects.count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Categories seeded: {total} total ({roots} new roots, {children} new children)."
            )
        )
