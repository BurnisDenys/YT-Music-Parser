from pydantic import BaseModel, EmailStr, field_validator

class User(BaseModel):
    name: str
    email: EmailStr
    account_id: int # робимо int, бо валідатор порівнює числа
    
    @field_validator("account_id")
    def validate_account_id(cls, value):
        if value <= 0:
            raise ValueError(f"account_id must be positive: {value}")
        return value

# Правильне створення об'єкта
user = User(name="Jack", email="jack@pixegami.io", account_id=10)
print(user)
