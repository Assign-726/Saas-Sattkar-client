"""Seed a realistic demo menu (30 categories, 220+ items) into one restaurant branch.

    python manage.py seed_menu                          # first restaurant, its first branch
    python manage.py seed_menu --restaurant minku-restaurant --branch spice-villa-main-branch

Safe to run again: categories, subcategories and items are matched by name, so nothing is
duplicated and items you have already edited are left alone.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Category, MenuItem, SubCategory
from super_admin.models import Restaurant, RestaurantBranch

ALLERGENS = {'D': 'Dairy/Milk', 'G': 'Gluten', 'P': 'Peanuts', 'T': 'Tree Nuts', 'E': 'Eggs',
             'F': 'Fish', 'C': 'Crustaceans', 'S': 'Soybeans'}
FOOD = {'v': 'veg', 'n': 'non_veg', 'e': 'egg'}

# category: (description, prep minutes, sizes, {subcategory: [(name, price, food, allergens, kcal), ...]})
# sizes: None | 'half' (Half/Full, half = 60%) | 'pizza' (Regular/Medium/Large) | 'glass' (Glass/Jug)
MENU = {
    'Soups': ('Slow-simmered soups served piping hot.', 10, None, {
        'Veg Soups': [('Tomato Shorba', 149, 'v', '', 110), ('Sweet Corn Veg Soup', 159, 'v', 'S', 140),
                      ('Hot & Sour Veg Soup', 159, 'v', 'S', 120), ('Manchow Veg Soup', 169, 'v', 'GS', 150),
                      ('Lemon Coriander Soup', 159, 'v', '', 90)],
        'Non-Veg Soups': [('Chicken Manchow Soup', 189, 'n', 'GS', 190), ('Chicken Hot & Sour Soup', 189, 'n', 'S', 170),
                          ('Mutton Paya Shorba', 249, 'n', '', 260)],
    }),
    'Salads': ('Fresh, crunchy and made to order.', 8, None, {
        'Salads': [('Green Salad', 99, 'v', '', 60), ('Kachumber Salad', 119, 'v', '', 70),
                   ('Greek Salad', 249, 'v', 'D', 220), ('Caesar Salad', 269, 'v', 'DGE', 310),
                   ('Chicken Caesar Salad', 329, 'n', 'DGE', 390), ('Sprouts Chaat Salad', 149, 'v', '', 160)],
    }),
    'Veg Starters': ('Crispy, smoky and spiced starters.', 15, None, {
        'Fried': [('Hara Bhara Kebab', 229, 'v', '', 280), ('Veg Spring Roll', 219, 'v', 'G', 300),
                  ('Crispy Corn Salt & Pepper', 229, 'v', '', 320), ('Dahi Ke Kebab', 249, 'v', 'D', 310)],
        'Chaat': [('Papdi Chaat', 149, 'v', 'DG', 280), ('Dahi Puri', 149, 'v', 'DG', 250),
                  ('Aloo Tikki Chaat', 159, 'v', 'D', 300)],
    }),
    'Non-Veg Starters': ('Signature meat and egg starters.', 18, None, {
        'Chicken': [('Chicken 65', 299, 'n', '', 420), ('Chicken Lollipop', 319, 'n', 'G', 460),
                    ('Amritsari Chicken Fry', 309, 'n', 'G', 440), ('Chicken Seekh Kebab', 329, 'n', '', 380)],
        'Egg': [('Egg Pakora', 169, 'e', 'E', 260), ('Egg Chilli Dry', 199, 'e', 'ES', 300)],
        'Mutton': [('Mutton Galouti Kebab', 429, 'n', '', 410), ('Mutton Shami Kebab', 399, 'n', 'E', 390)],
    }),
    'Tandoor Specials': ('Straight from the clay oven.', 22, 'half', {
        'Veg Tandoor': [('Paneer Tikka', 299, 'v', 'D', 360), ('Malai Soya Chaap', 279, 'v', 'DS', 380),
                        ('Tandoori Mushroom', 279, 'v', 'D', 220), ('Tandoori Aloo', 229, 'v', 'D', 260)],
        'Non-Veg Tandoor': [('Tandoori Chicken', 399, 'n', 'D', 520), ('Chicken Tikka', 349, 'n', 'D', 410),
                            ('Malai Chicken Tikka', 369, 'n', 'DT', 450), ('Fish Tikka', 449, 'n', 'DF', 360),
                            ('Tandoori Prawns', 549, 'n', 'DC', 340)],
    }),
    'Chinese Starters': ('Indo-Chinese wok tossed starters.', 15, None, {
        'Veg': [('Veg Manchurian Dry', 229, 'v', 'GS', 340), ('Chilli Paneer Dry', 269, 'v', 'DS', 380),
                ('Honey Chilli Potato', 219, 'v', 'S', 410), ('Chilli Mushroom', 249, 'v', 'S', 240)],
        'Non-Veg': [('Chilli Chicken Dry', 299, 'n', 'S', 420), ('Dragon Chicken', 319, 'n', 'ST', 450),
                    ('Garlic Prawns', 449, 'n', 'CS', 330)],
    }),
    'Paneer Specialities': ('Fresh cottage cheese, house made daily.', 18, 'half', {
        'Paneer': [('Paneer Butter Masala', 299, 'v', 'DT', 480), ('Kadai Paneer', 289, 'v', 'D', 420),
                   ('Palak Paneer', 279, 'v', 'D', 360), ('Shahi Paneer', 309, 'v', 'DT', 510),
                   ('Paneer Lababdar', 309, 'v', 'DT', 500), ('Matar Paneer', 269, 'v', 'D', 390),
                   ('Paneer Bhurji', 279, 'v', 'D', 410)],
    }),
    'Veg Main Course': ('Home-style and restaurant favourite curries.', 18, 'half', {
        'Curries': [('Mix Veg Curry', 249, 'v', 'D', 300), ('Malai Kofta', 289, 'v', 'DT', 520),
                    ('Veg Kolhapuri', 259, 'v', '', 330), ('Navratan Korma', 279, 'v', 'DT', 470)],
        'Dry Sabzi': [('Aloo Gobi', 219, 'v', '', 240), ('Bhindi Masala', 229, 'v', '', 210),
                      ('Jeera Aloo', 199, 'v', '', 260), ('Baingan Bharta', 229, 'v', '', 220),
                      ('Mushroom Do Pyaza', 259, 'v', 'D', 250)],
    }),
    'Chicken Main Course': ('Rich, slow-cooked chicken curries.', 22, 'half', {
        'Chicken Curries': [('Butter Chicken', 369, 'n', 'DT', 560), ('Chicken Tikka Masala', 369, 'n', 'D', 520),
                            ('Kadai Chicken', 349, 'n', '', 460), ('Chicken Curry Home Style', 329, 'n', '', 430),
                            ('Chicken Chettinad', 359, 'n', '', 470), ('Chicken Korma', 369, 'n', 'DT', 540),
                            ('Egg Curry', 229, 'e', 'E', 330)],
    }),
    'Mutton Main Course': ('Tender goat meat, cooked low and slow.', 30, 'half', {
        'Mutton Curries': [('Mutton Rogan Josh', 449, 'n', 'D', 560), ('Mutton Kadai', 439, 'n', '', 540),
                           ('Laal Maas', 469, 'n', '', 580), ('Mutton Keema Matar', 419, 'n', '', 500),
                           ('Mutton Bhuna', 449, 'n', '', 550)],
    }),
    'Seafood': ('Coastal catch, cooked fresh.', 22, None, {
        'Fish': [('Fish Curry Goan Style', 429, 'n', 'F', 420), ('Amritsari Fish', 399, 'n', 'FG', 450),
                 ('Fish Fingers', 349, 'n', 'FGE', 430), ('Pomfret Fry', 549, 'n', 'F', 380)],
        'Prawns': [('Prawn Masala', 499, 'n', 'C', 400), ('Prawn Malai Curry', 529, 'n', 'CD', 470)],
    }),
    'Dal & Kadhi': ('Comforting lentils and kadhi.', 15, 'half', {
        'Dal': [('Dal Makhani', 249, 'v', 'D', 420), ('Dal Tadka', 199, 'v', '', 300),
                ('Dal Fry', 189, 'v', '', 290), ('Dal Palak', 209, 'v', '', 280)],
        'Kadhi': [('Punjabi Kadhi Pakora', 219, 'v', 'D', 340), ('Rajma Masala', 219, 'v', '', 320),
                  ('Chole Masala', 219, 'v', '', 330)],
    }),
    'Biryani & Pulao': ('Dum-cooked biryanis with raita and salan.', 25, 'half', {
        'Biryani': [('Veg Dum Biryani', 279, 'v', 'D', 520), ('Paneer Biryani', 309, 'v', 'D', 580),
                    ('Chicken Dum Biryani', 349, 'n', 'D', 650), ('Hyderabadi Chicken Biryani', 369, 'n', 'D', 670),
                    ('Mutton Dum Biryani', 449, 'n', 'D', 720), ('Egg Biryani', 279, 'e', 'DE', 590),
                    ('Prawn Biryani', 499, 'n', 'DC', 620)],
        'Pulao': [('Veg Pulao', 219, 'v', '', 410), ('Kashmiri Pulao', 249, 'v', 'DT', 480)],
    }),
    'Rice': ('Steamed and tempered rice.', 10, None, {
        'Rice': [('Steamed Rice', 129, 'v', '', 260), ('Jeera Rice', 159, 'v', 'D', 300),
                 ('Ghee Rice', 179, 'v', 'D', 360), ('Curd Rice', 169, 'v', 'D', 310),
                 ('Lemon Rice', 159, 'v', 'P', 320)],
    }),
    'Indian Breads': ('Fresh from the tandoor and tawa.', 8, None, {
        'Tandoori Breads': [('Tandoori Roti', 30, 'v', 'G', 120), ('Butter Roti', 40, 'v', 'GD', 150),
                            ('Plain Naan', 50, 'v', 'GD', 260), ('Butter Naan', 60, 'v', 'GD', 290),
                            ('Garlic Naan', 75, 'v', 'GD', 300), ('Cheese Naan', 119, 'v', 'GD', 380),
                            ('Laccha Paratha', 70, 'v', 'GD', 320)],
        'Stuffed': [('Aloo Paratha', 99, 'v', 'GD', 340), ('Paneer Kulcha', 129, 'v', 'GD', 380),
                    ('Amritsari Kulcha', 119, 'v', 'GD', 360)],
    }),
    'Chinese Main Course': ('Gravies and rice from the wok.', 15, None, {
        'Gravy': [('Veg Manchurian Gravy', 249, 'v', 'GS', 350), ('Chilli Chicken Gravy', 319, 'n', 'S', 430),
                  ('Schezwan Paneer Gravy', 289, 'v', 'DS', 400)],
        'Fried Rice': [('Veg Fried Rice', 219, 'v', 'S', 420), ('Egg Fried Rice', 239, 'e', 'ES', 460),
                       ('Chicken Fried Rice', 269, 'n', 'ES', 500), ('Schezwan Fried Rice', 239, 'v', 'S', 450)],
    }),
    'Noodles': ('Hakka and Schezwan noodles.', 12, None, {
        'Noodles': [('Veg Hakka Noodles', 219, 'v', 'GS', 430), ('Chicken Hakka Noodles', 269, 'n', 'GSE', 510),
                    ('Schezwan Noodles', 229, 'v', 'GS', 450), ('Chilli Garlic Noodles', 229, 'v', 'GS', 440),
                    ('Singapore Noodles', 259, 'v', 'GS', 460)],
    }),
    'South Indian': ('Dosas, idlis and more with sambar and chutneys.', 12, None, {
        'Dosa': [('Plain Dosa', 129, 'v', '', 260), ('Masala Dosa', 159, 'v', '', 340),
                 ('Mysore Masala Dosa', 179, 'v', '', 370), ('Paneer Dosa', 199, 'v', 'D', 410),
                 ('Rava Masala Dosa', 179, 'v', 'G', 380)],
        'Idli & Vada': [('Idli Sambar', 109, 'v', '', 210), ('Medu Vada', 119, 'v', '', 280),
                        ('Uttapam Onion Tomato', 159, 'v', '', 320)],
    }),
    'Pizza': ('Hand-stretched, stone-baked pizzas.', 18, 'pizza', {
        'Veg Pizza': [('Margherita Pizza', 249, 'v', 'DG', 700), ('Farmhouse Pizza', 329, 'v', 'DG', 780),
                      ('Paneer Tikka Pizza', 359, 'v', 'DG', 820), ('Veggie Supreme Pizza', 349, 'v', 'DG', 800)],
        'Non-Veg Pizza': [('Chicken Tikka Pizza', 389, 'n', 'DG', 860), ('BBQ Chicken Pizza', 399, 'n', 'DGS', 880),
                          ('Pepperoni Pizza', 429, 'n', 'DG', 920)],
    }),
    'Pasta': ('Italian classics, cooked al dente.', 15, None, {
        'Pasta': [('Penne Arrabbiata', 279, 'v', 'G', 520), ('Penne Alfredo', 299, 'v', 'GD', 640),
                  ('Pink Sauce Pasta', 299, 'v', 'GD', 600), ('Spaghetti Aglio e Olio', 289, 'v', 'G', 540),
                  ('Chicken Alfredo Pasta', 349, 'n', 'GD', 720), ('Mac & Cheese', 289, 'v', 'GD', 680)],
    }),
    'Burgers & Sandwiches': ('Toasted, grilled and stacked.', 12, None, {
        'Burgers': [('Aloo Tikki Burger', 149, 'v', 'G', 420), ('Paneer Burger', 199, 'v', 'GD', 480),
                    ('Crispy Chicken Burger', 229, 'n', 'GE', 560), ('Double Cheese Veg Burger', 219, 'v', 'GD', 590)],
        'Sandwiches': [('Veg Grilled Sandwich', 159, 'v', 'GD', 380), ('Bombay Masala Sandwich', 149, 'v', 'GD', 360),
                       ('Club Sandwich', 249, 'n', 'GDE', 540), ('Paneer Tikka Sandwich', 199, 'v', 'GD', 430)],
    }),
    'Rolls & Wraps': ('Rumali and paratha rolls.', 12, None, {
        'Rolls': [('Paneer Tikka Roll', 199, 'v', 'GD', 450), ('Veg Kathi Roll', 169, 'v', 'G', 400),
                  ('Chicken Tikka Roll', 229, 'n', 'GD', 490), ('Egg Roll', 149, 'e', 'GE', 380),
                  ('Mutton Seekh Roll', 269, 'n', 'G', 520)],
    }),
    'Thali & Combos': ('Complete meals at great value.', 20, None, {
        'Thali': [('Mini Veg Thali', 249, 'v', 'DG', 750), ('Deluxe Veg Thali', 349, 'v', 'DG', 980),
                  ('Non-Veg Thali', 429, 'n', 'DG', 1100), ('Rajasthani Thali', 399, 'v', 'DG', 1050)],
        'Combos': [('Rajma Chawal Combo', 199, 'v', '', 560), ('Chole Bhature', 189, 'v', 'GD', 640),
                   ('Butter Chicken Rice Bowl', 299, 'n', 'D', 680)],
    }),
    'Raita & Accompaniments': ('Cool sides to go with every meal.', 5, None, {
        'Raita': [('Boondi Raita', 99, 'v', 'D', 150), ('Mix Veg Raita', 99, 'v', 'D', 130),
                  ('Plain Curd', 79, 'v', 'D', 110)],
        'Sides': [('Masala Papad', 69, 'v', '', 90), ('Roasted Papad', 39, 'v', '', 50),
                  ('French Fries', 129, 'v', '', 360), ('Peri Peri Fries', 149, 'v', '', 380)],
    }),
    'Desserts': ('Traditional sweets and modern desserts.', 6, None, {
        'Indian': [('Gulab Jamun (2 pcs)', 99, 'v', 'DG', 300), ('Rasmalai (2 pcs)', 129, 'v', 'DT', 280),
                   ('Gajar Ka Halwa', 149, 'v', 'DT', 350), ('Shahi Tukda', 159, 'v', 'DGT', 420),
                   ('Kulfi Falooda', 169, 'v', 'DT', 380)],
        'Western': [('Chocolate Brownie with Ice Cream', 199, 'e', 'DGE', 520), ('New York Cheesecake', 249, 'e', 'DGE', 450),
                    ('Tiramisu', 269, 'e', 'DGE', 480)],
    }),
    'Ice Creams & Sundaes': ('Scoops and sundaes.', 4, None, {
        'Scoops': [('Vanilla Scoop', 79, 'v', 'D', 140), ('Chocolate Scoop', 89, 'v', 'D', 160),
                   ('Butterscotch Scoop', 89, 'v', 'DT', 160), ('Mango Scoop', 89, 'v', 'D', 150)],
        'Sundaes': [('Hot Chocolate Fudge', 199, 'v', 'DT', 480), ('Brownie Sundae', 219, 'e', 'DGE', 540),
                    ('Banana Split', 209, 'v', 'DT', 500)],
    }),
    'Hot Beverages': ('Freshly brewed tea and coffee.', 5, None, {
        'Tea': [('Masala Chai', 49, 'v', 'D', 90), ('Ginger Lemon Tea', 59, 'v', '', 40),
                ('Green Tea', 69, 'v', '', 5)],
        'Coffee': [('Filter Coffee', 69, 'v', 'D', 110), ('Cappuccino', 129, 'v', 'D', 130),
                   ('Cafe Latte', 139, 'v', 'D', 150), ('Hot Chocolate', 149, 'v', 'D', 260)],
    }),
    'Mocktails': ('Refreshing house mocktails.', 5, 'glass', {
        'Mocktails': [('Virgin Mojito', 159, 'v', '', 120), ('Blue Lagoon', 169, 'v', '', 140),
                      ('Watermelon Cooler', 159, 'v', '', 110), ('Fruit Punch', 169, 'v', '', 150),
                      ('Kala Khatta', 139, 'v', '', 120), ('Fresh Lime Soda', 99, 'v', '', 80)],
    }),
    'Shakes & Smoothies': ('Thick shakes and fruit smoothies.', 6, None, {
        'Shakes': [('Chocolate Shake', 169, 'v', 'D', 420), ('Oreo Shake', 189, 'v', 'DG', 480),
                   ('Strawberry Shake', 169, 'v', 'D', 380), ('Cold Coffee', 149, 'v', 'D', 320)],
        'Smoothies': [('Banana Peanut Smoothie', 179, 'v', 'DP', 410), ('Mixed Berry Smoothie', 199, 'v', 'D', 300),
                      ('Mango Lassi', 129, 'v', 'D', 290), ('Sweet Lassi', 99, 'v', 'D', 260)],
    }),
    'Fresh Juices': ('Cold-pressed to order, no added sugar.', 5, 'glass', {
        'Juices': [('Orange Juice', 129, 'v', '', 110), ('Watermelon Juice', 109, 'v', '', 90),
                   ('Pineapple Juice', 119, 'v', '', 120), ('Mosambi Juice', 109, 'v', '', 100),
                   ('ABC Juice', 149, 'v', '', 130), ('Fresh Coconut Water', 89, 'v', '', 45)],
    }),
}

VEGAN_CATEGORIES = {'Fresh Juices', 'Mocktails'}


def variants_for(sizes, price):
    if sizes == 'half':
        return [{'type': 'Size', 'name': 'Half', 'price': float(round(price * Decimal('0.6')))},
                {'type': 'Size', 'name': 'Full', 'price': float(price)}]
    if sizes == 'pizza':
        return [{'type': 'Size', 'name': 'Regular', 'price': float(price)},
                {'type': 'Size', 'name': 'Medium', 'price': float(price + 150)},
                {'type': 'Size', 'name': 'Large', 'price': float(price + 300)},
                {'type': 'Extra', 'name': 'Extra Cheese', 'price': 60.0}]
    if sizes == 'glass':
        return [{'type': 'Size', 'name': 'Glass', 'price': float(price)},
                {'type': 'Size', 'name': 'Jug', 'price': float(price * 3)}]
    return []


class Command(BaseCommand):
    help = 'Seed a demo menu: 30 categories with subcategories and 220+ menu items.'

    def add_arguments(self, parser):
        parser.add_argument('--restaurant', help='Restaurant slug (default: the first restaurant)')
        parser.add_argument('--branch', help='Branch slug (default: the restaurant\'s first branch)')

    @transaction.atomic
    def handle(self, *args, **opts):
        qs = Restaurant.objects.order_by('pk')
        restaurant = qs.filter(slug=opts['restaurant']).first() if opts['restaurant'] else qs.first()
        if not restaurant:
            raise CommandError('Restaurant not found.')
        branches = RestaurantBranch.objects.filter(restaurant=restaurant).order_by('pk')
        branch = branches.filter(slug=opts['branch']).first() if opts['branch'] else branches.first()
        if opts['branch'] and not branch:
            raise CommandError('Branch not found for this restaurant.')

        # short codes continue after the highest numeric code already in use
        used = [int(c) for c in MenuItem.objects.filter(restaurant=restaurant).values_list('short_code', flat=True)
                if c and str(c).isdigit()]
        next_code = max([1000] + used) + 1

        made = {'categories': 0, 'subcategories': 0, 'items': 0, 'skipped': 0}
        for cat_name, (blurb, prep, sizes, subs) in MENU.items():
            cat, created = Category.objects.get_or_create(
                restaurant=restaurant, branch=branch, name__iexact=cat_name,
                defaults={'name': cat_name, 'description': blurb})
            made['categories'] += created
            for sub_name, items in subs.items():
                sub, created = SubCategory.objects.get_or_create(category=cat, name=sub_name)
                made['subcategories'] += created
                for name, price, food, allergen_codes, kcal in items:
                    if MenuItem.objects.filter(restaurant=restaurant, branch=branch, name__iexact=name).exists():
                        made['skipped'] += 1
                        continue
                    price = Decimal(price)
                    prefix = ''.join(ch for ch in name.upper() if ch.isalpha())[:3]
                    MenuItem.objects.create(
                        restaurant=restaurant, branch=branch, category=cat, subcategory=sub,
                        name=name, short_code=str(next_code), sku=f'{prefix}-{next_code}',
                        description=f'{name}. {blurb}', price=price,
                        food_type=FOOD[food], is_vegan=cat_name in VEGAN_CATEGORIES,
                        allergens=[ALLERGENS[a] for a in allergen_codes], calories=str(kcal),
                        portion_serves='2 People' if sizes == 'half' else '1 Person', preparation_time=prep,
                        publishing_channels=['pos', 'qr', 'online'], variants_json=variants_for(sizes, price),
                        markup_price=(price * Decimal('0.15')).quantize(Decimal('1')),
                        packaging_fee=Decimal('10') if price < 200 else Decimal('20'),
                        tax_type=MenuItem.TaxType.GST_FOOD, availability=True,
                    )
                    next_code += 1
                    made['items'] += 1

        where = f'{restaurant.slug} / {branch.slug if branch else "all branches"}'
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {where}: {made['categories']} categories, {made['subcategories']} subcategories, "
            f"{made['items']} items added ({made['skipped']} already existed)."))
