"""
Инициализация моделей базы данных.
Экспорт всех моделей для удобного импорта.
"""

from app.models.users import (
    Buyer, Seller, Admin,
    DeliveryService, BuyerDelivery, SellerDelivery,
    BaseUser
)
from app.models.products import (
    Category, Parameter, CategoryParameter,
    Product, ProductPhoto, ProductParameter,
    ProductCard, ModerationRemark, ProductEvent
)
from app.models.orders import (
    CartItem, Favorite, Order, OrderItem,
    Promotion, PromotionProduct, SellerPromotion, Bonus, Return, Banner
)
from app.models.communications import (
    Message, Review, Mailing, Settings, InfoPost, RoadmapEvent, EduMaterial
)
from app.models.loyalty import (
    LoyaltyRate, SellerLoyalty, BuyerBonus
)
from app.models.promo import (
    PromoCode
)
from app.models.tariffs import (
    SellerTariffSubscription, TariffTransaction
)
from app.models.footer import (
    FooterLink
)

# Экспорт всех моделей
__all__ = [
    # Пользователи
    'Buyer', 'Seller', 'Admin', 'BaseUser',
    'DeliveryService', 'BuyerDelivery', 'SellerDelivery',

    # Товары
    'Category', 'Parameter', 'CategoryParameter',
    'Product', 'ProductPhoto', 'ProductParameter',
    'ProductCard', 'ModerationRemark', 'ProductEvent',

    # Заказы
    'CartItem', 'Favorite', 'Order', 'OrderItem',
    'Promotion', 'PromotionProduct', 'SellerPromotion', 'Bonus', 'Return', 'Banner',

    # Коммуникации
    'Message', 'Review', 'Mailing', 'Settings', 'InfoPost', 'RoadmapEvent', 'EduMaterial',

    # Лояльность
    'LoyaltyRate', 'SellerLoyalty', 'BuyerBonus',

    # Промокоды
    'PromoCode',

    # Тарифы магазина (покупаемые селлерами)
    'SellerTariffSubscription', 'TariffTransaction',

    # Подвал
    'FooterLink',
]
